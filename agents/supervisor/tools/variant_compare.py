"""variant_compare tool — side-by-side variant comparison of a single model.

Given a model ID and 2–3 variant IDs, BatchGetItem fetches each VAR# item
from ``vsa-acquire-catalog`` and returns a structured comparison dict the
LLM can narrate.

**Semantic distinction from ``catalog_compare``:** this tool compares
variants *of a single model* (e.g. Standard vs Racing trim on the same
base model), whereas ``catalog_compare`` compares *across models*
(e.g. Model A vs Model B for a commuting use-case).

When ``acquire_catalog_table`` is not configured the tool returns an explicit
``{available: false}`` response.

Renamed from ``configurator_compare`` per spec
``2026-08-04-cvx-oem-discover-order-meridian`` (umbrella PRD Q3 resolution). See
``connected-mobility-guidance-on-aws/.kiro/specs/
2026-08-04-cvx-oem-discover-order-meridian/decisions.md`` for the rename
rationale.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

# Lazy DDB resource — shared with configurator_recommend but each module
# keeps its own reference to remain self-contained per the project pattern.
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


def _build_one_line_summary(entries: list[dict]) -> str:
    """Build a human-readable one-line tradeoff summary for the LLM."""
    if not entries:
        return "No variants to compare."
    if len(entries) == 1:
        entry = entries[0]
        return (
            f"{entry['displayName']} is available with a price delta of "
            f"{entry['priceDelta']}."
        )
    # Multi-variant: cheapest vs. most expensive
    by_price = sorted(entries, key=lambda e: e["priceDelta"])
    cheapest = by_price[0]["displayName"]
    priciest = by_price[-1]["displayName"]
    parts = [f"{cheapest} is the most affordable variant"]
    if cheapest != priciest:
        parts.append(f"{priciest} is the premium option")
    return "; ".join(parts) + "."


def make_variant_compare_tool(ctx: SessionContext, config: Config):
    """Build the variant_compare tool bound to a session context."""

    catalog_table: str = getattr(config, "acquire_catalog_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"

    @tool
    def variant_compare(
        modelId: str,
        variantIds: list[str],
    ) -> dict[str, Any]:
        """Compare 2–3 variants of a single catalog model side-by-side.

        Use this tool when the customer has already chosen a base model and
        wants to understand the differences between its available trim levels
        or variant options (e.g. Standard vs Racing vs Touring of the same
        model). For cross-model comparison (comparing different models),
        use catalog_compare instead.

        Args:
            modelId: The base model identifier whose variants to compare
                (the trailing segment of a MODEL# itemKey, e.g.
                ``"meridian-300"``).
            variantIds: List of 2–3 variant identifiers to compare (the
                trailing segment of a VAR# itemKey, e.g.
                ``["standard", "racing"]``). Must contain 2–3 items.

        Returns:
            Dict with ``comparison`` (list of per-variant dicts with specs
            and price delta) and ``summary`` (one-line tradeoff narrative).
        """
        if not catalog_table:
            return {"available": False, "reason": "catalog table not configured"}

        if len(variantIds) < 2 or len(variantIds) > 3:
            return {
                "available": False,
                "reason": f"variantIds must contain 2–3 items; got {len(variantIds)}",
            }

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(catalog_table)

            # Fetch MODEL# item to confirm the model exists and get base price.
            # contains(itemKey, ...) cannot go in FilterExpression because
            # itemKey is the sort key.  Use begins_with("MODEL#") in the
            # KeyConditionExpression to fetch all MODEL# items for the tenant,
            # then narrow to the exact modelId in Python.
            try:
                model_query_kwargs = dict(
                    KeyConditionExpression=(
                        "tenantId = :tid AND begins_with(itemKey, :model_prefix)"
                    ),
                    ExpressionAttributeValues={
                        ":tid": ctx.tenant_id,
                        ":model_prefix": "MODEL#",
                    },
                )
                all_model_pages: list[dict] = []
                while True:
                    scan_resp = table.query(**model_query_kwargs)
                    all_model_pages.extend(scan_resp.get("Items", []))
                    last = scan_resp.get("LastEvaluatedKey")
                    if not last:
                        break
                    model_query_kwargs["ExclusiveStartKey"] = last
            except (BotoCoreError, ClientError) as exc:
                log.warning("variant_compare: model query for %s failed (%s)", modelId, exc)
                return {"available": False, "reason": f"model lookup failed: {exc}"}

            model_item: dict | None = None
            for item in all_model_pages:
                item_key = item.get("itemKey", "")
                parts = item_key.split("#")
                if len(parts) >= 3 and parts[2].lower() == modelId.lower():
                    model_item = _decimal_to_float(item)
                    break

            if model_item is None:
                return {
                    "available": False,
                    "reason": f"model '{modelId}' not found in catalog",
                }

            # Fetch VAR# items for this model.
            # begins_with on sort key → KeyConditionExpression only.
            # Python post-filter narrows to variants of this modelId.
            try:
                var_query_kwargs = dict(
                    KeyConditionExpression=(
                        "tenantId = :tid AND begins_with(itemKey, :var_prefix)"
                    ),
                    ExpressionAttributeValues={
                        ":tid": ctx.tenant_id,
                        ":var_prefix": "VAR#",
                    },
                )
                all_var_pages: list[dict] = []
                while True:
                    var_resp = table.query(**var_query_kwargs)
                    all_var_pages.extend(var_resp.get("Items", []))
                    last = var_resp.get("LastEvaluatedKey")
                    if not last:
                        break
                    var_query_kwargs["ExclusiveStartKey"] = last
                all_variants = [
                    _decimal_to_float(i)
                    for i in all_var_pages
                    if f"#{modelId}#".lower() in i.get("itemKey", "").lower()
                ]
            except (BotoCoreError, ClientError) as exc:
                log.warning("variant_compare: variant query for %s failed (%s)", modelId, exc)
                all_variants = []

            # Index variants by their trailing variantId segment.
            variant_map: dict[str, dict] = {}
            for v in all_variants:
                item_key = v.get("itemKey", "")
                # VAR#<categoryId>#<modelId>#<variantId>
                key_parts = item_key.split("#")
                if len(key_parts) >= 4:
                    variant_map[key_parts[-1]] = v

        except (BotoCoreError, ClientError) as exc:
            log.warning("variant_compare: DDB access failed (%s)", exc)
            return {"available": False, "reason": f"catalog lookup failed: {exc}"}

        comparison = []
        for variant_id in variantIds:
            v = variant_map.get(variant_id)
            if v is None:
                comparison.append({
                    "variantId": variant_id,
                    "found": False,
                })
                continue

            comparison.append({
                "variantId": variant_id,
                "found": True,
                "displayName": v.get("displayName", variant_id),
                "priceDelta": v.get("priceDelta", 0),
                "specs": v.get("specs") or {},
            })

        found_entries = [e for e in comparison if e.get("found")]

        return {
            "modelId": modelId,
            "modelDisplayName": model_item.get("displayName", modelId),
            "basePrice": model_item.get("basePrice", 0),
            "comparison": comparison,
            "summary": _build_one_line_summary(found_entries),
        }

    return variant_compare
