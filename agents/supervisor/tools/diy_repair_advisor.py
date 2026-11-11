"""DIY repair advisor — augmented with Bedrock KB dtc_guide snippets (CVX-on-foundation v1).

KEEPS the existing rule-based sub-agent corpus path (owner-manuals).
AUGMENTS: after rule-based lookup, calls bedrock-agent-runtime:Retrieve
with source_category=dtc_guide to append KB-derived DTC snippets to the
response. On KB Retrieve failure, falls back silently to rule-based only.

DS-2 (2026-10-05): Added a no-DIY gate before any lookup. Returns a
structured refusal for P0/P1 sessions and for DTC codes not on the TPMS
allowlist.

See spec: .kiro/specs/2026-06-15-cvx-on-foundation-v1/spec.md § "Tool repointing"
"""

from __future__ import annotations

import logging
import re
from typing import Any

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import ClientError
from strands import tool

from agents.supervisor.session import SessionContext
from agents.supervisor.tools.kb_filter import apply_source_category_filter
from agents.supervisor.tools.sub_agent import (
    SubAgentConfig,
    make_sub_agent_tool,
)

log = logging.getLogger(__name__)

_BOTOCORE_CFG = BotocoreConfig(read_timeout=8)

# ---------------------------------------------------------------------------
# No-DIY gate (DS-2, 2026-10-05)
# ---------------------------------------------------------------------------
#
# Before any sub-agent or KB call, refuse DIY guidance when:
#   1. The session's classifier verdict is P0 or P1 (unsafe to DIY).
#   2. The query or dtc_code contains a DTC code not on the explicit
#      allowlist.
#
# The allowlist starts with tire-pressure (TPMS) codes the severity table
# marks "safe to drive" — code classes where a driver can reasonably check
# the pressure themselves. Engine codes, brake codes, airbag codes, and
# anything else are refused; every such fault class is serviced, not
# self-repaired on the road.
#
# A query with no code and no P0/P1 verdict passes through today's behaviour.

# DTC-shaped token: letter class + 4 hex digits, not inside a longer word or code.
# Also matches the catalog's suffixed codes (B0001_FIRE, U3000_CRASH): the code
# part is followed by "_", which is not a hex digit.
# SAE shape: system letter, then 0-3, then three hex digits, so English words
# such as "paced" or "ceded" are not read as codes.
_DTC_PATTERN = re.compile(r"(?<![A-Za-z0-9])[PCBUpcbu][0-3][0-9A-Fa-f]{3}(?![0-9A-Fa-f])")

# Codes for which DIY guidance is allowed. The report allows only tire-pressure
# (TPMS) codes that lambdas/shared/dtc_severity.yaml marks safe to drive; that table
# has no tire-pressure code (checked 2026-10-05), so the list is empty and every
# code is refused. Tire pressure, washer fluid and the other quick fixes are still
# reachable by describing the symptom, with no code.
_DIY_ALLOWLIST: frozenset[str] = frozenset()

# Verdicts at which no DIY guidance is given at all.
_NO_DIY_PRIORITIES = frozenset({"P0", "P1"})

_DIY_BLOCKED_RESPONSE: dict[str, Any] = {
    "diyAllowed": False,
    "reason": (
        "Do-it-yourself guidance is not available for this fault or severity. "
        "Engine, brake, airbag and other warning-light faults need a service "
        "technician."
    ),
    "nextStep": "Do not give do-it-yourself steps. Offer to find a service center.",
}


def _extract_dtc_codes(query: str, dtc_code: str) -> list[str]:
    """DTC codes named in ``dtc_code`` or ``query``, uppercased.

    A non-empty ``dtc_code`` always counts as a code, whatever its shape.
    """
    found: set[str] = set()
    raw = str(dtc_code or "").strip()
    if raw:
        found.add(raw.upper())
    for m in _DTC_PATTERN.finditer(str(query or "")):
        found.add(m.group().upper())
    return sorted(found)


def _diy_gate_check(
    ctx: SessionContext | None,
    query: str,
    dtc_code: str,
) -> dict[str, Any] | None:
    """A refusal when DIY guidance must not be given; None to proceed.

    Refuses when the session's classifier verdict, or a driver-confirmed
    emergency, is P0 or P1; when the driver has REPORTED fire/smoke or brake loss
    (whatever the classifier says — a refusal costs only words, so it does not wait
    for the confirmation the booking waits for); or when any DTC code is named that
    is not on ``_DIY_ALLOWLIST``. Otherwise None (today's behaviour).
    """
    if getattr(ctx, "driver_reported_hazard", False):
        return dict(_DIY_BLOCKED_RESPONSE)
    for attr in ("triage_classification", "driver_confirmed_priority"):
        if getattr(ctx, attr, None) in _NO_DIY_PRIORITIES:
            return dict(_DIY_BLOCKED_RESPONSE)
    codes = _extract_dtc_codes(query, dtc_code)
    if any(c not in _DIY_ALLOWLIST for c in codes):
        return dict(_DIY_BLOCKED_RESPONSE)
    return None

_TIRE_TRIGGERS = (
    "tire", "tires", "tyre", "tyres", "psi", "pressure",
    "inflate", "deflate", "tpms", "flat",
)


def _tire_pressure_chat_message(
    *,
    query: str,
    dtc_code: str = "",
    vehicle_phrase: str = "",
    driver_first: str = "",
    **_kwargs,
) -> str | None:
    """Return a markdown chat message with F-150 tire pressures, or None."""
    q = (query or "").lower()
    if not any(t in q for t in _TIRE_TRIGGERS):
        return None
    title_vehicle = vehicle_phrase or "Ford F-150"
    return (
        f"**🛞 {title_vehicle} — Cold Tire Pressures**\n"
        "\n"
        "• Front: **35 PSI**\n"
        "• Rear: **35 PSI**\n"
        "• Spare (compact): 60 PSI\n"
        "\n"
        "_Always verify against the sticker on your driver's-side door jamb. "
        "Add 5 PSI rear when towing or hauling heavy loads._"
    )


def _kb_dtc_snippets(query: str, adp_kb_id: str, adp_region: str) -> list[dict[str, Any]]:
    """Retrieve DTC-guide snippets from KB; return empty list on any failure."""
    if not adp_kb_id:
        return []
    try:
        client = boto3.client(
            "bedrock-agent-runtime",
            region_name=adp_region,
            config=_BOTOCORE_CFG,
        )
        resp = client.retrieve(
            knowledgeBaseId=adp_kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": apply_source_category_filter(
                    {"numberOfResults": 3},
                    {"equals": {"key": "source_category", "value": "dtc_guide"}},
                )
            },
        )
        results = resp.get("retrievalResults") or []
        return [
            {
                "text": r.get("content", {}).get("text", ""),
                "source": r.get("location", {}).get("s3Location", {}).get("uri", "kb"),
                "score": r.get("score", 0),
            }
            for r in results
        ]
    except (ClientError, Exception) as exc:  # noqa: BLE001
        log.info("diy_repair_advisor: KB dtc_guide Retrieve failed (%s) — using rule-based only", exc)
        return []


def make_diy_repair_advisor_tool(ctx: SessionContext | None = None, config: Any = None):
    """Build the diy_repair_advisor sub-agent tool, augmented with KB dtc_guide snippets."""
    adp_kb_id: str = getattr(config, "adp_kb_id", "") or ""
    adp_region: str = getattr(config, "adp_region", None) or "us-east-1"

    # Build the underlying rule-based sub-agent tool.
    sub_agent_cfg = SubAgentConfig(
        name="diy_repair_advisor",
        description=(
            "Use this when the driver asks if they can fix something "
            "themselves, wants step-by-step repair guidance, or asks "
            "how to handle a vehicle warning that might be a quick fix "
            "(e.g. 'Can I top this off myself?', 'Is this something "
            "I can handle?', 'Walk me through it'). Returns owner-manual "
            "reference material — you should summarize the relevant "
            "section in 1-2 short sentences for the driver."
        ),
        corpus_subdir="owner-manuals",
        chat_message_fn=_tire_pressure_chat_message,
    )
    _rule_based_tool_fn = make_sub_agent_tool(sub_agent_cfg, ctx=ctx)

    @tool(name="diy_repair_advisor")
    def diy_repair_advisor(query: str, dtc_code: str = "") -> dict[str, Any]:
        """Advise the driver on DIY repair and DTC interpretation.

        Returns owner-manual reference material plus any relevant technical
        service bulletins from the vehicle knowledge base.

        Args:
            query: The driver's repair question or vehicle symptom description.
            dtc_code: Optional DTC fault code (e.g. "P0420").
        """
        # No-DIY gate (driver-safety batch 2026-10-05): before any lookup, refuse
        # on a P0/P1 verdict or a named fault code. The prompt's "no safe DIY"
        # rule did not hold (2026-09-25); this is the enforcement point.
        blocked = _diy_gate_check(ctx, query, dtc_code)
        if blocked is not None:
            log.info("diy_repair_advisor: refused by the no-DIY gate")
            return blocked

        # Step 1: rule-based corpus lookup (always runs).
        base_result: dict[str, Any] = _rule_based_tool_fn(query=query, dtc_code=dtc_code)

        # Step 2: augment with KB dtc_guide snippets (silent on failure).
        kb_query = f"{dtc_code} {query}".strip() if dtc_code else query
        kb_snippets = _kb_dtc_snippets(kb_query, adp_kb_id, adp_region)
        if kb_snippets:
            base_result["kb_dtc_snippets"] = kb_snippets

        return base_result

    return diy_repair_advisor
