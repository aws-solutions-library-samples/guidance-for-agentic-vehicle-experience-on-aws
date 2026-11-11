"""catalog_query tool — open-ended Discover-phase catalog search.

Reads the tenant catalog from ``vsa-acquire-catalog`` via the by-category
GSI and returns the top-3 models that best match the stated criteria and
optional budget/priority hint.

**Semantic distinction from ``configurator_recommend``:** ``catalog_query``
is the Discover-phase entry point for an anonymous prospect browsing
broadly ("I need something for city commuting"), whereas
``configurator_recommend`` is used mid-configurator when the customer has
already chosen a category and budget and wants a model within those
constraints. Both read the same catalog; the docstring context guides the
LLM to call the right one at the right step.

When ``acquire_catalog_table`` is not configured the tool returns an
explicit ``{available: false}`` response.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

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
    criteria: str,
    budget_max: Optional[int],
    priority_hint: Optional[str],
) -> Optional[float]:
    """Return a relevance score for a catalog MODEL# item, or None to exclude."""
    base_price = float(item.get("basePrice") or 0)
    if budget_max is not None and base_price > budget_max:
        return None

    if priority_hint == "electric":
        item_key: str = item.get("itemKey", "")
        category_fragment = item_key.split("#")[1] if "#" in item_key else ""
        if "electric" not in category_fragment.lower():
            return None

    tagline: str = str(item.get("tagline") or "")
    specs_raw = item.get("specs") or {}
    specs_text = " ".join(
        f"{k} {v}"
        for k, v in (specs_raw.items() if isinstance(specs_raw, dict) else [])
    )
    search_corpus = (tagline + " " + specs_text).lower()
    tokens = [t for t in criteria.lower().split() if len(t) > 2]
    keyword_score = sum(1 for token in tokens if token in search_corpus)

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


def make_catalog_query_tool(ctx: SessionContext, config: Config):
    """Build the catalog_query tool bound to a session context."""

    catalog_table: str = getattr(config, "acquire_catalog_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"

    @tool
    def catalog_query(
        criteria: str,
        budgetMax: Optional[int] = None,
        priorityHint: Optional[str] = None,
    ) -> dict[str, Any]:
        """Search the vehicle catalog for models matching a natural-language description.

        Use this tool at the start of a shopping conversation when the
        customer describes what they need — a use-case, lifestyle, or
        broad preference — and you want to surface matching models from
        the catalog. Returns up to 3 models ranked by relevance.

        For narrowing down *between specific models already shortlisted*,
        use catalog_compare. For in-configurator recommendation once the
        customer has chosen a category and budget, use
        configurator_recommend instead.

        Args:
            criteria: Natural-language description of what the customer
                needs, e.g. "daily city commuting under 20 km", "weekend
                adventure rides", "electric vehicle for urban use".
            budgetMax: Optional maximum budget in tenant currency units.
                Models priced above this value are excluded.
            priorityHint: Optional broad preference keyword:
                "comfort", "efficiency", "adventure", or "electric".

        Returns:
            Dict with key ``models`` — list of up to 3 items, each with
            ``modelId``, ``displayName``, ``basePrice``, ``tagline``,
            ``matchReasoning``; and ``available`` (bool).
        """
        if not catalog_table:
            return {"available": False, "reason": "catalog table not configured"}

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(catalog_table)

            # Query all MODEL# items for this tenant.
            # begins_with on the sort key (itemKey) MUST be in
            # KeyConditionExpression — DynamoDB forbids primary-key
            # attributes in FilterExpression.
            query_kwargs = dict(
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
                scan_resp = table.query(**query_kwargs)
                model_items.extend(scan_resp.get("Items", []))
                last = scan_resp.get("LastEvaluatedKey")
                if not last:
                    break
                query_kwargs["ExclusiveStartKey"] = last
        except (BotoCoreError, ClientError) as exc:
            log.warning("catalog_query: DDB scan failed (%s)", exc)
            return {"available": False, "reason": f"catalog lookup failed: {exc}"}

        scored: list[tuple[float, dict]] = []
        for item in model_items:
            item = _decimal_to_float(item)
            score = _score_model(item, criteria, budgetMax, priorityHint)
            if score is not None:
                scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        top3 = scored[:3]

        models = []
        for score, item in top3:
            item_key = item.get("itemKey", "")
            parts = item_key.split("#")
            model_id = parts[2] if len(parts) >= 3 else item_key
            reason_parts = []
            if score > 0:
                reason_parts.append(f"matches '{criteria}' search criteria")
            if budgetMax is not None:
                reason_parts.append(f"within budget of {budgetMax}")
            if priorityHint:
                reason_parts.append(f"aligns with {priorityHint} preference")
            match_reasoning = "; ".join(reason_parts) if reason_parts else "general catalog match"
            models.append({
                "modelId": model_id,
                "displayName": item.get("displayName", model_id),
                "basePrice": item.get("basePrice", 0),
                "tagline": item.get("tagline", ""),
                "matchReasoning": match_reasoning,
            })

        return {"available": True, "models": models}

    return catalog_query
