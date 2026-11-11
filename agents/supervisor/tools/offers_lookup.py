"""offers_lookup tool — retrieve promotional offers for a catalog model.

Reads ``OFFER#<modelId>#<offerId>`` items from ``vsa-acquire-catalog``
and returns active offers filtered by a deterministic validity-window
check (``validFrom ≤ now ≤ validUntil``).

Every returned offer includes a **required** ``disclosure_txt`` field
that the LLM must present verbatim when narrating the offer to the
customer.

The ``guardrails_identifier`` from config is included on the tool
response for downstream narration attachment. In v1 this identifier is
a placeholder (``discover-offers-guardrails-v1``); policy authoring is
a future spec.

When ``acquire_catalog_table`` is not configured the tool returns an
explicit ``{available: false}`` response.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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


def _is_offer_active(item: dict, now_iso: str) -> bool:
    """Return True if the offer's validity window covers now_iso."""
    valid_from = item.get("validFrom", "")
    valid_until = item.get("validUntil", "")
    if not valid_from or not valid_until:
        # Treat missing window as always-active (lenient for seed data).
        return True
    try:
        return valid_from <= now_iso <= valid_until
    except Exception:
        return True


def make_offers_lookup_tool(ctx: SessionContext, config: Config):
    """Build the offers_lookup tool bound to a session context."""

    catalog_table: str = getattr(config, "acquire_catalog_table", "") or ""
    aws_region: str = getattr(config, "aws_region", "us-west-2") or "us-west-2"
    guardrails_identifier: str = (
        getattr(config, "discover_guardrails_identifier", "")
        or "discover-offers-guardrails-v1"
    )

    @tool
    def offers_lookup(
        modelId: str,
        region: Optional[str] = None,
    ) -> dict[str, Any]:
        """Retrieve current promotional offers for a specific catalog model.

        Use this tool when the customer asks about current deals, offers,
        discounts, financing promotions, or limited-time incentives for a
        model. Always present the ``disclosure_txt`` field verbatim when
        narrating any offer — do not paraphrase or omit it.

        Args:
            modelId: The model identifier to look up offers for
                (e.g. ``"meridian-300"``).
            region: Optional ISO-3166 region code to filter offers
                (e.g. ``"IN"``). When omitted, all active offers are
                returned.

        Returns:
            Dict with key ``offers`` (list of active offer objects, each
            including ``offerId``, ``displayName``, ``body``,
            ``depositAmount``, ``validFrom``, ``validUntil``,
            ``disclosure_txt``, and optionally ``heroImageKey``);
            ``available`` (bool); and ``guardrailsIdentifier`` for
            downstream narration gating.
        """
        if not catalog_table:
            return {"available": False, "reason": "catalog table not configured"}

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            ddb = _get_ddb(aws_region)
            table = ddb.Table(catalog_table)

            # Query OFFER# items for this model using SK begins_with pattern.
            #
            # Paginated to match the sibling catalog tools. The prefix is narrow
            # so a single page is the realistic case, but a tenant with long
            # `body` / `disclosure_txt` values could exceed DynamoDB's 1 MB page
            # limit — and silently dropping an offer is the worst possible
            # failure here, because a missing offer takes its mandatory
            # `disclosure_txt` with it.
            query_kwargs: dict = {
                "KeyConditionExpression": (
                    "tenantId = :tid AND begins_with(itemKey, :sk_prefix)"
                ),
                "ExpressionAttributeValues": {
                    ":tid": ctx.tenant_id,
                    ":sk_prefix": f"OFFER#{modelId}#",
                },
            }
            raw_items = []
            while True:
                resp = table.query(**query_kwargs)
                raw_items.extend(resp.get("Items", []))
                last_key = resp.get("LastEvaluatedKey")
                if not last_key:
                    break
                query_kwargs["ExclusiveStartKey"] = last_key
        except (BotoCoreError, ClientError) as exc:
            log.warning("offers_lookup: DDB query failed for modelId=%s (%s)", modelId, exc)
            return {"available": False, "reason": f"offers lookup failed: {exc}"}

        offers = []
        for item in raw_items:
            item = _decimal_to_float(item)

            # Validity-window filter — deterministic, not wall-clock dependent.
            if not _is_offer_active(item, now_iso):
                continue

            # Region filter — only apply when region is specified and item has regions.
            item_regions = item.get("regions")
            if region and item_regions and region not in item_regions:
                continue

            # disclosure_txt is required; skip malformed items silently.
            disclosure_txt = item.get("disclosure_txt", "")

            item_key = item.get("itemKey", "")
            # OFFER#<modelId>#<offerId> → last segment is offerId
            offer_id = item_key.split("#")[-1] if "#" in item_key else item_key

            offers.append({
                "offerId": offer_id,
                "displayName": item.get("displayName", ""),
                "body": item.get("body", ""),
                "depositAmount": item.get("depositAmount", 0),
                "validFrom": item.get("validFrom", ""),
                "validUntil": item.get("validUntil", ""),
                "heroImageKey": item.get("heroImageKey"),
                "disclosure_txt": disclosure_txt,
            })

        return {
            "available": True,
            "modelId": modelId,
            "offers": offers,
            "guardrailsIdentifier": guardrails_identifier,
        }

    return offers_lookup
