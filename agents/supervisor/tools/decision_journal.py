"""decision_journal tool — wraps GET /api/v1/decision-journal on the CMS API.

Returns the most recent operator decisions from the decision journal DDB table,
sorted descending by timestamp (newest first, up to 50 entries). Each entry is
a raw DDB item with Decimal values converted to float by the CMS handler; the
schema is flexible and fields may be absent on sparse rows.

JWT and CMS base URL come from the SessionContext and Config respectively —
not from the LLM. The LLM calls this tool with no arguments.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from strands import tool

from agents.supervisor.config import Config
from agents.supervisor.session import SessionContext
from agents.supervisor.tools.fleet_errors import FleetToolAuthError, FleetToolError

log = logging.getLogger(__name__)

# Path suffix — the only URL fragment hard-coded in this module.
_ENDPOINT_PATH = "/api/v1/decision-journal"


def make_decision_journal_tool(ctx: SessionContext, config: Config):
    """Build the decision_journal tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover it.
    It takes no LLM-visible arguments — the JWT and CMS base URL are
    closure-captured from ctx and config.

    Args:
        ctx: Active session context; ctx.jwt is forwarded as Bearer token.
        config: Supervisor config; config.cms_api_base is the CMS API base URL.

    Returns:
        A @tool-decorated callable that returns a decision journal dict.
    """
    _jwt = ctx.jwt
    _base = config.cms_api_base

    @tool
    def decision_journal() -> dict[str, Any]:
        """Retrieve the fleet decision journal — the most recent operator decisions
        on fleet actions, sorted newest-first. Each entry records what was decided
        (APPROVED or REJECTED), which vehicle was involved, when the decision was
        made, who made it, and any supporting reasoning from the agent.

        The journal schema is flexible; entries may have varying fields depending
        on how they were written. Common fields: actionId, decision, agentResponse,
        vehicleId, resolvedBy, timestamp (Unix epoch seconds).

        Call this tool when the fleet manager asks what decisions have been made
        recently, wants a history of approvals or rejections, or asks who acted
        on a particular recommendation.

        Args:
            (none — fleet scope is determined by the caller's Cognito identity)

        Returns:
            A dict with keys:
              available (bool): True on success, False when CMS is not configured.
              decisions (list[dict]): Up to 50 decision journal entries, newest
                first. Each entry may include: actionId (str), decision (str),
                agentResponse (str), vehicleId (str), resolvedBy (str),
                timestamp (int, Unix epoch seconds). Fields may be absent on
                sparse rows — access defensively.
            Or {"available": False, "reason": "CMS API not configured"} when
            config.cms_api_base is empty.
        """
        if not _base:
            return {"available": False, "reason": "CMS API not configured"}

        url = f"{_base}{_ENDPOINT_PATH}"
        log.info("decision_journal: GET %s", url)

        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {_jwt}",
                "Content-Type": "application/json",
            },
            timeout=12,
        )

        if resp.status_code == 401:
            raise FleetToolAuthError(
                "CMS decision-journal endpoint returned 401. JWT may have expired."
            )
        if resp.status_code >= 400:
            raise FleetToolError(
                f"CMS decision-journal endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        body = resp.json()
        return {
            "available": True,
            "decisions": body.get("decisions", []),
        }

    return decision_journal
