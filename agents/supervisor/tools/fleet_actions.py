"""fleet_actions tool — wraps GET /api/v1/fleet-actions on the CMS API.

Returns a read-only list of fleet actions from the action queue. Supports an
optional status filter (default PENDING). Each action has an actionId, type,
description, domain, priority, severity, status, and optional vehicleId/VIN.

This tool is READ-ONLY at v1. The approve/reject write path (POST
/api/v1/fleet-actions/{actionId}/approve|reject) is explicitly out of scope
for this version — see spec.md § Constraints. Do NOT add write operations here
without a separate spec and security review.

JWT and CMS base URL come from the SessionContext and Config respectively —
not from the LLM. The only LLM-visible parameter is the optional status filter.
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
_ENDPOINT_PATH = "/api/v1/fleet-actions"


def make_fleet_actions_tool(ctx: SessionContext, config: Config):
    """Build the fleet_actions tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover it.
    It accepts an optional status filter visible to the LLM. The JWT and CMS
    base URL are closure-captured from ctx and config.

    Args:
        ctx: Active session context; ctx.jwt is forwarded as Bearer token.
        config: Supervisor config; config.cms_api_base is the CMS API base URL.

    Returns:
        A @tool-decorated callable that returns a fleet actions dict.
    """
    _jwt = ctx.jwt
    _base = config.cms_api_base

    @tool
    def fleet_actions(status: str = "PENDING") -> dict[str, Any]:
        """Retrieve fleet actions from the action queue. Returns up to 50 actions
        sorted by creation time (newest first). Each action represents a pending,
        approved, or rejected recommendation from the fleet management system —
        such as scheduling an inspection, dispatching roadside assistance, or
        reviewing a diagnostics alert.

        This tool is READ-ONLY. It does not approve, reject, or modify actions.

        Call this tool when the fleet manager asks about pending actions, what
        needs to be reviewed, or wants a list of recent decisions.

        Args:
            status: Filter by action status. One of "PENDING" (default), "APPROVED",
                "REJECTED", or "ALL". Use "ALL" to see all recent actions regardless
                of status.

        Returns:
            A dict with keys:
              available (bool): True on success, False when CMS is not configured.
              actions (list[dict]): Up to 50 action objects, each with:
                actionId (str): Unique identifier.
                actionType (str): e.g. "INSPECTION", "ROADSIDE_DISPATCH", "TOWING".
                agentResponse (str): Human-readable description of the recommendation.
                createdAt (str): ISO 8601 creation timestamp.
                domain (str): Category — "Driver Escalation", "Diagnostics", etc.
                priority (str): "CRITICAL", "HIGH", "MEDIUM", or "LOW".
                resolvedAt (str|None): ISO 8601 resolution time or null.
                severity (str): Same canonical set as priority.
                status (str): "PENDING", "APPROVED", or "REJECTED".
                vehicleId (str|None): Associated vehicle ID if available.
                vin (str|None): Associated VIN if available.
              total (int): Total matching actions before the 50-item limit.
            Or {"available": False, "reason": "CMS API not configured"} when
            config.cms_api_base is empty.
        """
        if not _base:
            return {"available": False, "reason": "CMS API not configured"}

        url = f"{_base}{_ENDPOINT_PATH}"
        log.info("fleet_actions: GET %s status=%s", url, status)

        resp = requests.get(
            url,
            params={"status": status},
            headers={
                "Authorization": f"Bearer {_jwt}",
                "Content-Type": "application/json",
            },
            timeout=12,
        )

        if resp.status_code == 401:
            raise FleetToolAuthError(
                "CMS fleet-actions endpoint returned 401. JWT may have expired."
            )
        if resp.status_code >= 400:
            raise FleetToolError(
                f"CMS fleet-actions endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        body = resp.json()
        return {
            "available": True,
            "actions": body.get("actions", []),
            "total": body.get("total", 0),
        }

    return fleet_actions
