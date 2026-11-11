"""catalog_compare tool — cross-model comparison for the Discover phase.

Given 2–3 model IDs, BatchGetItem (via per-model queries) fetches each
MODEL# item plus child VAR# items from ``vsa-acquire-catalog`` and returns
a structured comparison dict the LLM can narrate as tradeoffs.

**Semantic distinction from ``variant_compare``:** ``catalog_compare``
compares *different models* (e.g. comparing an adventure model against an
electric model for daily commuting), whereas ``variant_compare`` compares
*variants of a single model* (e.g. Standard vs Racing trim on the same
base model). The LLM should select the right tool based on whether the
customer is still choosing between models or has already picked one.

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


def _build_one_line_summary(entries: list[dict], criteria: Optional[str]) -> str:
    """Build a human-readable one-line tradeoff summary for the LLM."""
    if not entries:
        return "No models to compare."
    if len(entries) == 1:
        entry = entries[0]
        return (
            f"{entry['displayName']} is available at "
            f"{entry['basePrice']} with {len(entry['variants'])} variant(s)."
        )
    by_price = sorted(entries, key=lambda e: e["basePrice"])
    by_variants = sorted(entries, key=lambda e: len(e["variants"]), reverse=True)
    cheapest = by_price[0]["displayName"]
    most_variants = by_variants[0]["displayName"]
    parts = [f"{cheapest} is the most affordable"]
    if cheapest != most_variants:
        parts.append(f"{most_variants} offers the most variant choices")
    if criteria:
        parts.append(f"considering your need for '{criteria}'")
    return "; ".join(parts) + "."


def make_catalog_compare_tool(ctx: SessionContext, config: Config):
    """Build the catalog_compare tool bound to a session context."""

    catalog_table: str = getattr(config, "acquire_catalog_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"

    @tool
    def catalog_compare(
        modelIds: list[str],
        criteria: Optional[str] = None,
    ) -> dict[str, Any]:
        """Compare 2–3 catalog models side-by-side for a Discover-phase decision.

        Use this tool when the customer has narrowed down to 2–3 candidate
        models and wants a structured comparison to help choose between them.
        This compares *different models* — for comparing variants (trims) of
        the same model, use variant_compare instead.

        Args:
            modelIds: List of 2–3 model identifiers to compare (the trailing
                segment of a MODEL# itemKey, e.g.
                ``["meridian-300", "solstice-e"]``). Must contain 2–3 items.
            criteria: Optional context about what matters to the customer,
                e.g. "daily commuting", "range for long trips". Used to
                surface relevant tradeoffs in the summary.

        Returns:
            Dict with ``comparison`` (list of per-model dicts with specs and
            variants), ``summary`` (one-line tradeoff narrative contextualised
            by criteria), and ``available`` (bool).
        """
        if not catalog_table:
            return {"available": False, "reason": "catalog table not configured"}

        if len(modelIds) < 2 or len(modelIds) > 3:
            return {
                "available": False,
                "reason": f"modelIds must contain 2–3 items; got {len(modelIds)}",
            }

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(catalog_table)

            model_items: dict[str, dict] = {}
            variant_items: dict[str, list[dict]] = {}

            for model_id in modelIds:
                try:
                    # Use begins_with on the sort key in KeyConditionExpression —
                    # DynamoDB forbids primary-key attributes in FilterExpression.
                    # MODEL# items have the shape MODEL#<category>#<modelId>, so
                    # begins_with(itemKey, "MODEL#") is the most specific prefix
                    # available as a key condition.  We then narrow to the exact
                    # modelId in Python over the returned items.
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
                    log.warning(
                        "catalog_compare: query for %s failed (%s)", model_id, exc
                    )
                    continue

                for item in all_model_pages:
                    item_key = item.get("itemKey", "")
                    parts = item_key.split("#")
                    if len(parts) >= 3 and parts[2].lower() == model_id.lower():
                        model_items[model_id] = _decimal_to_float(item)
                        break

                try:
                    # begins_with on sort key → KeyConditionExpression.
                    # Python post-filter narrows to variants for this model_id.
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
                    variants_for_model = [
                        _decimal_to_float(i)
                        for i in all_var_pages
                        if f"#{model_id}#".lower() in i.get("itemKey", "").lower()
                    ]
                    variant_items[model_id] = variants_for_model
                except (BotoCoreError, ClientError) as exc:
                    log.warning(
                        "catalog_compare: variant query for %s failed (%s)", model_id, exc
                    )
                    variant_items[model_id] = []

        except (BotoCoreError, ClientError) as exc:
            log.warning("catalog_compare: DDB access failed (%s)", exc)
            return {"available": False, "reason": f"catalog lookup failed: {exc}"}

        comparison = []
        for model_id in modelIds:
            item = model_items.get(model_id)
            if item is None:
                comparison.append({"modelId": model_id, "found": False})
                continue

            variants = [
                {
                    "variantId": (
                        i.get("itemKey", "").split("#")[-1]
                        if i.get("itemKey")
                        else model_id
                    ),
                    "displayName": i.get("displayName", ""),
                    "priceDelta": i.get("priceDelta", 0),
                }
                for i in variant_items.get(model_id, [])
            ]

            comparison.append({
                "modelId": model_id,
                "found": True,
                "displayName": item.get("displayName", model_id),
                "basePrice": item.get("basePrice", 0),
                "specs": item.get("specs") or {},
                "variants": variants,
            })

        found_entries = [e for e in comparison if e.get("found")]

        return {
            "available": True,
            "comparison": comparison,
            "summary": _build_one_line_summary(found_entries, criteria),
        }

    return catalog_compare
