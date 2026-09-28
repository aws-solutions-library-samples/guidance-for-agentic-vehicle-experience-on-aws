"""DMS warranty_agent tools — thin readers over DMS's rules-engine verdict shape.

These tools narrate DMS's warranty rules verdict VERBATIM.
A warranty denial is a money decision; softening 'denied' into 'may require
review' crosses the deterministic-seam boundary. See spec § Agentic tier
classification Q4 and agentic-tiers.md § "What stays deterministic, and why".

NO LLM call inside any tool — see tools/sub_agent.py docstring for why (voice
latency budget). These are fast DynamoDB reads / HTTP GETs to the DMS API.

Table: ``dms-{stage}-warranty-claims`` (DynamoDB, cross-account IAM only).
Region: us-west-2 (see spec Constraints — DMS decisions.md 2026-08-28 records
that us-east-1 was the wrong default, so we pin us-west-2 for DMS reads).

**Verdict schema — DMS-side authoritative.**  Verified against
``~/guidance-for-dealer-management-system-on-aws/source/handlers/warranty_claims.py``
which is what actually writes the record.  The rules engine's ``Verdict``
value is decomposed into THREE fields when persisted:

  - ``verdict_status``  → ``"approved" | "denied" | "requires_review"``
  - ``verdict_reason``  → the human-readable reason string
  - ``matched_rule_id`` → the ``Rule.id`` that determined the verdict
                          (or ``"NO_RULE_MATCH"`` when no rule matched)

The record's top-level ``status`` field is the claim's LIFECYCLE state
(``"Draft"`` / ``"Validated"`` / ``"Submitted"``) — NOT the verdict.

**Two gates, in order.**

  1. Certification-tier gate (spec § Constraint 4).  Consults the *caller's*
     dealer tier via ``check_dealer_certification`` before any read.  Below
     Gold → deterministic non-AI response.  Skips DDB entirely on ineligible
     callers.  This is a per-caller entitlement, not a per-target property.
  2. Post-read authorization scope check (spec § Constraints — "the agent
     does not widen scope").  After the DDB read, verifies the claim's
     ``dealer_id`` is in the caller's ``dealer_ids`` claim set.  Out of
     scope → ``no_access_response()`` — same shape as "not found" so a
     caller cannot enumerate claims by comparing responses.

The tier gate is caller-scoped; the scope check is target-scoped.  These
are complementary — cert-gate refuses AI narration to un-certified callers;
scope check refuses data disclosure to un-authorized callers.
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
_CLAIMS_TABLE_TPL = "dms-{stage}-warranty-claims"

# Public response shape when the DDB read fails at the transport / IAM layer.
# Boto3 exception strings can embed table ARNs (account + region + resource
# name) and IAM policy names; we log the diagnostic detail via log.warning
# but return a generic message that the LLM can safely narrate.
_UNAVAILABLE_RESPONSE: dict[str, Any] = {
    "available": False,
    "reason": "DMS table temporarily unreachable",
}


def _claims_table() -> str:
    return _CLAIMS_TABLE_TPL.format(stage=_DMS_STAGE)


def make_warranty_validate_tool(ctx: SessionContext, config: Config):
    """Build the warranty ``validate_claim`` tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover it.
    ``ctx`` and ``config`` are kept in scope; the DDB read is IAM-only.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that validates a warranty claim.
    """
    _table = _claims_table()

    @tool
    def validate_claim(claim_id: str) -> dict[str, Any]:
        """Look up and narrate the DMS warranty rules-engine verdict for a
        specific claim.  The verdict text — including any 'denied' decision
        and its reason — is returned VERBATIM as stored by the rules engine.

        IMPORTANT: Do NOT soften, paraphrase, or omit any part of a 'denied'
        verdict or its reason string when narrating to the user.  Warranty
        denial is a financial decision; the verbatim ruling is the required
        output.

        Args:
            claim_id: The DMS warranty claim identifier to look up
                      (e.g. "WC-2026-001234").

        Returns:
            A dict.  When the caller is AI-activated (Gold or Platinum)
            AND authorized for the claim's dealer:
              - available: True
              - claim_id, verdict_status, verdict_reason, matched_rule_id
                (verbatim); claim_state, amount, updated_at.

            Otherwise:
              - Below-Gold: ``deterministic_response_for_tier(tier)``.
              - Out-of-scope: ``no_access_response()`` (indistinguishable
                shape from "not found").
              - Not found: ``{"available": False, "reason": "Claim not found"}``.
              - Transport failure: ``{"available": False,
                                     "reason": "DMS table temporarily unreachable"}``.
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
            resp = tbl.get_item(Key={"claim_id": claim_id})
        except (BotoCoreError, ClientError) as exc:
            log.warning("validate_claim: DDB error for %s: %s", claim_id, exc)
            return _UNAVAILABLE_RESPONSE

        item = resp.get("Item")
        if not item:
            # Return the same shape as the out-of-scope response so an
            # authenticated Gold-tier caller cannot distinguish "no such
            # claim" from "someone else's claim" by comparing reason
            # strings (which would be an existence-enumeration primitive
            # across the DMS claim ID space).
            return no_access_response()

        # Gate 2: scope — post-read.  A warranty claim's authoritative scope
        # key is its own dealer_id.  A caller who lacks that dealer in claims
        # receives the "no access" shape — identical to any other unauthorized
        # target response.
        item_dealer_id = item.get("dealer_id", "")
        if not dealer_in_scope(ctx, item_dealer_id):
            return no_access_response(item_dealer_id)

        # Verbatim: pass the DMS-written fields through unchanged.
        return {
            "available": True,
            "claim_id": claim_id,
            "verdict_status": item.get("verdict_status", "unknown"),
            "verdict_reason": item.get("verdict_reason", ""),
            "matched_rule_id": item.get("matched_rule_id", ""),
            "claim_state": item.get("status"),
            "amount": item.get("amount"),
            "updated_at": item.get("updated_at"),
        }

    return validate_claim


def make_warranty_suggest_tool(ctx: SessionContext, config: Config):
    """Build the warranty ``suggest_correction`` tool bound to a session context.

    Returns rule-derived hints for a claim (e.g. missing documentation, wrong
    part code) WITHOUT reinventing warranty logic.  The hints come from the
    rules engine's stored correction hints; this tool only reads them.

    Args:
        ctx: Active session context (held for uniform factory signature).
        config: Supervisor config (held for uniform factory signature).

    Returns:
        A @tool-decorated callable that returns correction hints.
    """
    _table = _claims_table()

    @tool
    def suggest_correction(claim_id: str) -> dict[str, Any]:
        """Retrieve the DMS rules-engine correction hints for a warranty claim.
        These hints list specific issues the rules engine flagged so the dealer
        can correct the submission.

        Narrate hints as presented; do not invent additional suggestions.

        Args:
            claim_id: The DMS warranty claim identifier to look up.

        Returns:
            Below-Gold / out-of-scope: same shapes as ``validate_claim``.
            When AI-activated and authorized and hints were found:
              - available: True
              - claim_id, hints, matched_rule_id, updated_at.
            Not found: ``{"available": False, "reason": "Claim not found"}``.
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
            resp = tbl.get_item(Key={"claim_id": claim_id})
        except (BotoCoreError, ClientError) as exc:
            log.warning("suggest_correction: DDB error for %s: %s", claim_id, exc)
            return _UNAVAILABLE_RESPONSE

        item = resp.get("Item")
        if not item:
            # Enumeration-safe: same shape as no-access.  See validate_claim
            # for rationale.
            return no_access_response()

        # Gate 2: scope — same shape as validate_claim.
        item_dealer_id = item.get("dealer_id", "")
        if not dealer_in_scope(ctx, item_dealer_id):
            return no_access_response(item_dealer_id)

        return {
            "available": True,
            "claim_id": claim_id,
            "hints": item.get("correction_hints", []),
            "matched_rule_id": item.get("matched_rule_id", ""),
            "updated_at": item.get("updated_at"),
        }

    return suggest_correction
