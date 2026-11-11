"""configurator_recommend tool — catalog-backed model recommendation.

Reads the tenant's catalog from ``vsa-acquire-catalog`` via the ``by-category``
GSI and returns the top-3 models that best fit the stated use case and budget.

Ranking is deterministic (keyword match + budget filter + priorityHint reweight)
so the same inputs always return the same ordering — no random tiebreakers.

When ``acquire_catalog_table`` is not configured the tool returns an explicit
``{available: false}`` response identical to the pattern used in ``knowledge.py``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Literal, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

# Lazy DDB resource — same pattern as book.py
_ddb_resource: Any = None


def _get_ddb(region: str = "us-west-2"):
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=region)
    return _ddb_resource


def _decimal_to_float(value: Any) -> Any:
    """Recursively convert Decimal values returned by DDB to float."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_float(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_float(v) for v in value]
    return value


def _score_model(
    item: dict[str, Any],
    use_case: str,
    budget_max: Optional[int],
    priority_hint: Optional[str],
) -> Optional[float]:
    """Return a relevance score for a catalog MODEL# item.

    Returns None to exclude the item (e.g. over budget or mismatched
    priority_hint). Higher scores are returned first.
    """
    # Budget filter — hard exclusion
    base_price = float(item.get("basePrice") or item.get("base_price") or 0)
    if budget_max is not None and base_price > budget_max:
        return None

    # priorityHint filter — only include items from matching category
    if priority_hint:
        item_key: str = item.get("itemKey", "") or item.get("item_key", "")
        # itemKey format: MODEL#<categoryId>#<modelId>
        # "electric" hint → restrict to CAT#electric items only
        if priority_hint == "electric":
            category_fragment = item_key.split("#")[1] if "#" in item_key else ""
            if "electric" not in category_fragment.lower():
                return None
        # "efficiency", "comfort", "adventure" bias scores via keyword matching below

    # Keyword scoring — match use_case tokens against tagline + specs text
    tagline: str = str(item.get("tagline") or "")
    specs_raw = item.get("specs") or {}
    specs_text = " ".join(f"{k} {v}" for k, v in (specs_raw.items() if isinstance(specs_raw, dict) else []))
    search_corpus = (tagline + " " + specs_text).lower()

    tokens = [t for t in use_case.lower().split() if len(t) > 2]
    keyword_score = sum(1 for token in tokens if token in search_corpus)

    # priorityHint soft boost on top of keyword score
    hint_bonus = 0.0
    if priority_hint:
        hint_map = {
            "comfort": ["comfort", "touring", "smooth", "ergonomic"],
            "efficiency": ["fuel", "economy", "mileage", "efficient", "range"],
            "adventure": ["adventure", "trail", "offroad", "all-terrain"],
            "electric": ["electric", "ev", "battery", "range_km"],
        }
        boost_keywords = hint_map.get(priority_hint, [])
        hint_bonus = sum(1 for kw in boost_keywords if kw in search_corpus)

    return float(keyword_score) + hint_bonus * 0.5


def make_configurator_recommend_tool(ctx: SessionContext, config: Config):
    """Build the configurator_recommend tool bound to a session context."""

    catalog_table: str = getattr(config, "acquire_catalog_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"

    @tool
    def configurator_recommend(
        useCase: str,
        budgetMax: Optional[int] = None,
        priorityHint: Optional[Literal["comfort", "efficiency", "adventure", "electric"]] = None,
    ) -> dict[str, Any]:
        """Recommend up to 3 catalog models for a given use case and budget.

        Use this tool when the customer wants help choosing a vehicle but
        hasn't decided on a specific model. Returns a ranked shortlist with
        a brief reason for each recommendation.

        Args:
            useCase: Natural-language description of the intended use, e.g.
                "city commuting", "weekend adventure rides", "daily school run".
            budgetMax: Optional maximum budget (in tenant currency units).
                Models priced above this value are excluded.
            priorityHint: Optional preference keyword that biases the ranking:
                "comfort" (touring / ergonomic trim), "efficiency" (fuel /
                range optimized), "adventure" (off-road capable),
                "electric" (EV / battery-powered only).

        Returns:
            Dict with key ``recommendations`` — list of up to 3 items, each
            with ``modelId``, ``displayName``, ``basePrice``, ``tagline``, and
            ``matchReasoning``.
        """
        if not catalog_table:
            return {"available": False, "reason": "catalog table not configured"}

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(catalog_table)

            # Query by-category GSI to get all MODEL# items for this tenant
            resp = table.query(
                IndexName="by-category",
                KeyConditionExpression="tenantId = :tid AND begins_with(categoryKey, :prefix)",
                ExpressionAttributeValues={
                    ":tid": ctx.tenant_id,
                    ":prefix": "CAT#",
                },
            )
            category_keys = [item["itemKey"] for item in resp.get("Items", []) if item.get("itemKey", "").startswith("CAT#")]
        except (BotoCoreError, ClientError) as exc:
            log.warning("configurator_recommend: GSI query failed (%s)", exc)
            return {"available": False, "reason": f"catalog lookup failed: {exc}"}

        if not category_keys:
            return {"recommendations": []}

        # BatchGetItem for all MODEL# items in these categories
        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(catalog_table)
            # begins_with on the sort key (itemKey) MUST be in
            # KeyConditionExpression — DynamoDB forbids primary-key
            # attributes in FilterExpression.
            model_query_kwargs = dict(
                KeyConditionExpression=(
                    "tenantId = :tid AND begins_with(itemKey, :prefix)"
                ),
                ExpressionAttributeValues={
                    ":tid": ctx.tenant_id,
                    ":prefix": "MODEL#",
                },
            )
            model_items: list[dict] = []
            while True:
                scan_resp = table.query(**model_query_kwargs)
                model_items.extend(scan_resp.get("Items", []))
                last = scan_resp.get("LastEvaluatedKey")
                if not last:
                    break
                model_query_kwargs["ExclusiveStartKey"] = last
        except (BotoCoreError, ClientError) as exc:
            log.warning("configurator_recommend: model scan failed (%s)", exc)
            return {"available": False, "reason": f"model lookup failed: {exc}"}

        # Score and rank
        scored: list[tuple[float, dict]] = []
        for item in model_items:
            item = _decimal_to_float(item)
            score = _score_model(item, useCase, budgetMax, priorityHint)
            if score is not None:
                scored.append((score, item))

        # Sort descending; stable sort preserves DDB natural order on ties
        scored.sort(key=lambda x: x[0], reverse=True)
        top3 = scored[:3]

        recommendations = []
        for score, item in top3:
            item_key = item.get("itemKey", "")
            parts = item_key.split("#")
            model_id = parts[2] if len(parts) >= 3 else item_key
            reason_parts = []
            if score > 0:
                reason_parts.append(f"matches '{useCase}' keyword criteria")
            if budgetMax is not None:
                reason_parts.append(f"within budget of {budgetMax}")
            if priorityHint:
                reason_parts.append(f"aligns with {priorityHint} preference")
            match_reasoning = "; ".join(reason_parts) if reason_parts else "general match"
            recommendations.append({
                "modelId": model_id,
                "displayName": item.get("displayName", model_id),
                "basePrice": item.get("basePrice", 0),
                "tagline": item.get("tagline", ""),
                "matchReasoning": match_reasoning,
            })

        return {"recommendations": recommendations}

    return configurator_recommend
