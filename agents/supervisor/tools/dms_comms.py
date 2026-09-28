"""DMS comms_triage tools — communications triage, thin readers.

``classify_bulletin()`` reads a pre-computed classification from the DMS
communications table.  It does NOT invoke an LLM to classify — see
tools/sub_agent.py docstring for why (voice latency budget).  The Tier 2
classifier writes the record asynchronously; a bulletin that hasn't yet been
classified returns ``{"available": False, "reason": "bulletin not yet
classified"}`` — never a client-side fallback classification, which would
silently move the workload back to Tier 1.

Table: ``dms-{stage}-oem-communications`` (DynamoDB, cross-account IAM only,
us-west-2 per spec Constraints).  Verified 2026-09-04 against
``~/guidance-for-dealer-management-system-on-aws/deployment/stacks/dms_storage_stack.py:178``
— DMS's authoritative name for this surface is ``oem-communications`` (not
``comms-bulletins``, which the earlier code invented).

**Two gates, in order.**

  1. Certification-tier gate — caller-scoped (see ``dms_warranty.py`` docstring).
  2. Scope check — post-read.  A bulletin's authoritative scope key is its
     own ``dealer_id`` (bulletins are addressed to a specific dealer /
     dealer group).  Broadcast bulletins with an empty ``dealer_id`` are
     considered in-scope for any caller with a non-empty ``dealer_ids``
     claim set.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from agents.supervisor._lib.dms_cert_cache import (
    check_dealer_certification,
    dealer_in_scope,
    deterministic_response_for_tier,
    is_ai_activated,
    no_access_response,
    resolve_dealer_id,
)
from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext

log = logging.getLogger(__name__)

_DMS_STAGE = os.environ.get("VSA_DMS_STAGE", "staging")
_DMS_REGION = os.environ.get("VSA_DMS_REGION", "us-west-2")
# DMS's authoritative table name — NOT `comms-bulletins` which does not exist.
_BULLETINS_TABLE_TPL = "dms-{stage}-oem-communications"

_UNAVAILABLE_RESPONSE: dict[str, Any] = {
    "available": False,
    "reason": "DMS table temporarily unreachable",
}


def _bulletins_table() -> str:
    return _BULLETINS_TABLE_TPL.format(stage=_DMS_STAGE)


def _bulletin_in_scope(ctx: SessionContext, item: dict[str, Any]) -> bool:
    """A bulletin is in scope when its ``dealer_id`` is authorized OR it's a
    broadcast (empty ``dealer_id``) and the caller has any dealer at all.

    Broadcast handling matters because service-campaign / recall bulletins
    typically address every affected dealer; the alternative — refusing to
    narrate broadcasts to anyone — would defeat the tool's purpose.  A
    caller with an empty ``dealer_ids`` claim set is refused everything
    (fail-closed under the current no-claims reality).
    """
    scope = getattr(ctx, "dealer_ids", None) or []
    if not isinstance(scope, (list, tuple)) or not scope:
        return False
    dealer_id = item.get("dealer_id", "")
    if not dealer_id:
        # Broadcast — any authenticated dealer sees it.
        return True
    return dealer_in_scope(ctx, dealer_id)


def make_classify_bulletin_tool(ctx: SessionContext, config: Config):
    """Build the ``classify_bulletin`` tool bound to a session context.

    Reads a pre-computed classification for a DMS communications bulletin.
    Does NOT classify in real time — classification is a Tier 2 operation.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns a bulletin classification.
    """
    _table = _bulletins_table()

    @tool
    def classify_bulletin(bulletin_id: str) -> dict[str, Any]:
        """Retrieve the pre-computed classification and routing for a DMS
        communications bulletin.  Bulletins are classified by the DMS
        pipeline into categories (e.g. "service-campaign", "technical-update",
        "recall", "training", "incentive") with urgency levels and target
        audience lists.

        Args:
            bulletin_id: The DMS bulletin identifier (e.g. "BUL-2026-04321").

        Returns:
            The classification when AI-activated AND the bulletin's scope
            (broadcast, or the caller's authorized dealer) permits.
            Below-Gold: deterministic.  Out-of-scope / not found:
            same-shape refusal.  Not yet classified: honest deferral.
        """
        # Gate 1: cert-tier — caller-scoped.
        tier, _ = check_dealer_certification(resolve_dealer_id(ctx))
        if not is_ai_activated(tier):
            return deterministic_response_for_tier(tier)

        if not _table:
            return {"available": False, "reason": "DMS table not configured"}

        try:
            ddb = boto3.resource("dynamodb", region_name=_DMS_REGION)
            tbl = ddb.Table(_table)
            resp = tbl.get_item(Key={"bulletin_id": bulletin_id})
        except (BotoCoreError, ClientError) as exc:
            log.warning("classify_bulletin: DDB error for %s: %s", bulletin_id, exc)
            return _UNAVAILABLE_RESPONSE

        item = resp.get("Item")
        if not item:
            # Enumeration-safe: same shape as no-access.  Prevents an
            # authenticated Gold-tier caller from probing bulletin IDs by
            # comparing "not found" vs "no access" reason strings.
            return no_access_response()

        # Gate 2: scope — post-read.  A bulletin addressed to another dealer
        # is out of scope; a broadcast is in scope for any authenticated
        # dealer.  Same shape as no_access to prevent enumeration.
        if not _bulletin_in_scope(ctx, item):
            return no_access_response(item.get("dealer_id", ""))

        # An item without a Tier-2-written classification is legal (the item
        # was created but the classifier hasn't run yet); surface honestly
        # rather than fabricating a client-side classification.
        if not item.get("category") and not item.get("classification"):
            return {
                "available": False,
                "reason": "bulletin not yet classified",
                "bulletin_id": bulletin_id,
            }

        return {
            "available": True,
            "bulletin_id": bulletin_id,
            "category": item.get("category", "other"),
            "urgency": item.get("urgency", "informational"),
            "audience": item.get("audience", []),
            "summary": item.get("summary", ""),
            "action_required": bool(item.get("action_required", False)),
            "computed_at": item.get("computed_at"),
        }

    return classify_bulletin
