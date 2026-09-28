"""fleet_health tool — wraps GET /api/v1/fleet-health on the CMS API.

Returns a composite fleet health score (0–100) plus per-dimension scores
(utilization, cost_health, safety_compliance, maintenance_health) and a
details block with raw counts. The CMS endpoint has a 5-minute server-side
cache; the returned dict includes an 'available' flag so the LLM can narrate
gracefully if the service is not configured.

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

# Re-export so callers can do `from agents.supervisor.tools.fleet_health import FleetToolAuthError`
# (matches the test skeleton's import at test_fleet_tools.py Group G).
__all__ = [
    "make_fleet_health_tool",
    "FleetToolError",
    "FleetToolAuthError",
]

log = logging.getLogger(__name__)

# Path suffix — the only URL fragment hard-coded in this module.
_ENDPOINT_PATH = "/api/v1/fleet-health"


def make_fleet_health_tool(ctx: SessionContext, config: Config):
    """Build the fleet_health tool bound to a session context.

    The returned function is decorated with @tool so Strands can discover it.
    It takes no LLM-visible arguments — the JWT and CMS base URL are
    closure-captured from ctx and config.

    Args:
        ctx: Active session context; ctx.jwt is forwarded as Bearer token.
        config: Supervisor config; config.cms_api_base is the CMS API base URL.

    Returns:
        A @tool-decorated callable that returns a fleet health dict.
    """
    _jwt = ctx.jwt
    _base = config.cms_api_base

    @tool
    def fleet_health() -> dict[str, Any]:
        """Retrieve the current composite fleet health score and per-dimension
        scores from the fleet management system. The composite score is 0–100;
        sub-scores cover utilization, cost, safety compliance, and maintenance.
        Also returns a details block with raw vehicle counts, open alerts, and
        service-record totals.

        Call this tool when the fleet manager asks about overall fleet health,
        which vehicles need attention, or how the fleet is performing across
        any of the scored dimensions.

        Args:
            (none — fleet scope is determined by the caller's Cognito identity)

        Returns:
            A dict with keys:
              available (bool): True on success, False when CMS is not configured.
              composite (int): 0–100 weighted composite score.
              utilization (int): 0–100 utilization score.
              cost_health (int): 0–100 cost health score.
              safety_compliance (int): 0–100 safety compliance score.
              maintenance_health (int): 0–100 maintenance health score.
              details (dict): Raw counts — total_vehicles, active_vehicles,
                utilization_pct, open_critical_alerts, total_safety_events,
                total_maintenance_open, total_service_records, warranty_claims,
                warranty_paid, recovery_rate.
            Or {"available": False, "reason": "CMS API not configured"} when
            config.cms_api_base is empty.
        """
        if not _base:
            return {"available": False, "reason": "CMS API not configured"}

        url = f"{_base}{_ENDPOINT_PATH}"
        log.info("fleet_health: GET %s", url)

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
                "CMS fleet-health endpoint returned 401. JWT may have expired."
            )
        if resp.status_code >= 400:
            raise FleetToolError(
                f"CMS fleet-health endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        body = resp.json()
        return {
            "available": True,
            "composite": body.get("composite"),
            "utilization": body.get("utilization"),
            "cost_health": body.get("cost_health"),
            "safety_compliance": body.get("safety_compliance"),
            "maintenance_health": body.get("maintenance_health"),
            "details": body.get("details", {}),
        }

    return fleet_health
