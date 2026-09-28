"""daily_briefing tool — wraps GET /api/v1/daily-briefing on the CMS API.

Returns a structured daily summary for the fleet — a list of briefing items
(label, text, type) generated server-side from fleet health, open actions,
utilization, recalls, and warranty data. The CMS endpoint has a 15-minute
server-side cache.

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
_ENDPOINT_PATH = "/api/v1/daily-briefing"


def make_daily_briefing_tool(ctx: SessionContext, config: Config):
    """Build the daily_briefing tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover it.
    It takes no LLM-visible arguments — the JWT and CMS base URL are
    closure-captured from ctx and config.

    Args:
        ctx: Active session context; ctx.jwt is forwarded as Bearer token.
        config: Supervisor config; config.cms_api_base is the CMS API base URL.

    Returns:
        A @tool-decorated callable that returns a daily briefing dict.
    """
    _jwt = ctx.jwt
    _base = config.cms_api_base

    @tool
    def daily_briefing() -> dict[str, Any]:
        """Retrieve the daily fleet briefing — a structured summary of the most
        important fleet events, alerts, and metrics for today. Each item has a
        label (e.g. "Fleet Health", "Top Priority", "Utilization"), a human-
        readable text sentence, and a type ("success", "warning", "error", or
        "info"). The briefing is generated server-side from fleet health,
        open actions, utilization, recalls, and warranty data.

        Call this tool when the fleet manager starts their day and asks for a
        summary, or asks what needs attention today.

        Args:
            (none — fleet scope is determined by the caller's Cognito identity)

        Returns:
            A dict with keys:
              available (bool): True on success, False when CMS is not configured.
              generatedAt (str): ISO 8601 UTC timestamp of briefing generation.
              date (str): Human-readable date string (e.g. "August 21, 2026").
              items (list[dict]): Ordered briefing items, each with:
                label (str): Section heading.
                text (str): Human-readable summary sentence.
                type (str): One of "success", "warning", "error", "info".
            Or {"available": False, "reason": "CMS API not configured"} when
            config.cms_api_base is empty.
        """
        if not _base:
            return {"available": False, "reason": "CMS API not configured"}

        url = f"{_base}{_ENDPOINT_PATH}"
        log.info("daily_briefing: GET %s", url)

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
                "CMS daily-briefing endpoint returned 401. JWT may have expired."
            )
        if resp.status_code >= 400:
            raise FleetToolError(
                f"CMS daily-briefing endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        body = resp.json()
        return {
            "available": True,
            "generatedAt": body.get("generatedAt"),
            "date": body.get("date"),
            "items": body.get("items", []),
        }

    return daily_briefing
