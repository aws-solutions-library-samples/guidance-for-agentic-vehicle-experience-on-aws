"""Fleet Intelligence tools for the fleet_manager persona.

Replace the four tools that called CMS `/api/v1/fleet-health`, `/daily-briefing`,
`/fleet-actions` and `/decision-journal`. CMS removed those routes on
2026-09-06 with the VFO retirement (CMS spec `2026-09-05-cms-vfo-teardown`), so
the tools had nothing to call. The fleet view now lives in the CMS Fleet
Intelligence API (`services/fleet_intelligence/` in the CMS repo). See
issues/2026-09-29-fleet-manager-tools-call-removed-cms-routes/.

- `fleet_lifecycle` → GET /api/v1/fleet-intelligence/lifecycle
- `fleet_cost_outliers` → GET /api/v1/fleet-intelligence/cpm/outliers

Contract:

- **No model-chosen scope.** The caller's token is forwarded and Fleet
  Intelligence decides access from its claims (cross-fleet groups see every
  fleet; scoped groups only their own). When the user is on a fleet detail
  page, the fleet comes from the screen context (`screen_context.entities.
  fleetId`, already sanitized by `screen_context.py`); the server still checks
  it against the token, so the value grants nothing by itself.
- **Deterministic reads, no LLM.** The arithmetic (crossover month, cost per
  mile) is done by Fleet Intelligence. The tools trim and pass it on; they do
  not re-derive thresholds, so the summary counts stay Fleet Intelligence's.
- **Provenance and staleness travel with the data** (`provenance`,
  `computedAt`), so the agent can say "simulated" and "as of".
- **Failures are explicit.** Every non-success returns `available: false` with
  a reason the agent can say, never an empty result that reads as "nothing
  flagged".
- **Text surface only.** The lifecycle read takes about 2.6 s on staging, too
  slow for Nova Sonic's ~1 s tool budget. `fleet_manager` is a web-chat persona.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests
from strands import tool

log = logging.getLogger(__name__)

_LIFECYCLE_PATH = "/api/v1/fleet-intelligence/lifecycle"
_OUTLIERS_PATH = "/api/v1/fleet-intelligence/cpm/outliers"
_TIMEOUT_S = 15
_MAX_VEHICLES = 10
_FLEET_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_NOT_CONFIGURED = "Fleet data is not configured in this environment."
_UNREACHABLE = "Could not read fleet data right now."
_FORBIDDEN = ("This account cannot see fleet-wide data. Open a specific fleet's "
              "page to ask about that fleet.")
_FORBIDDEN_FLEET = "This account does not have access to that fleet's data."
_EXPIRED = "The sign-in has expired. Sign in again to read fleet data."
_NO_LIFECYCLE_DATA = "There is no lifecycle data for this fleet yet."
_NO_COST_DATA = "There is no cost data for this fleet yet."


def _screen_fleet_id(ctx: Any) -> str | None:
    sc = getattr(ctx, "screen_context", None) or {}
    fid = (sc.get("entities") or {}).get("fleetId") if isinstance(sc, dict) else None
    return fid if isinstance(fid, str) and _FLEET_ID_RE.match(fid) else None


def _round(v: Any, nd: int = 2) -> Any:
    return round(v, nd) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def _get(ctx: Any, config: Any, path: str, name: str) -> tuple[dict | None, dict | None, str | None]:
    """GET a Fleet Intelligence route. Returns (body, unavailable_result, fleet_id)."""
    base = (getattr(config, "cms_api_base", "") or "").rstrip("/")
    if not base:
        return None, {"available": False, "reason": _NOT_CONFIGURED}, None
    fleet_id = _screen_fleet_id(ctx)
    params = {"fleetId": fleet_id} if fleet_id else {}
    try:
        resp = requests.get(
            f"{base}{path}",
            params=params,
            headers={"Authorization": f"Bearer {getattr(ctx, 'jwt', '')}"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        log.warning("%s: request failed: %s", name, type(exc).__name__)
        return None, {"available": False, "reason": _UNREACHABLE}, fleet_id
    log.info("%s: GET %s fleet=%s status=%s", name, path, fleet_id or "-", resp.status_code)
    if resp.status_code == 401:
        return None, {"available": False, "reason": _EXPIRED}, fleet_id
    if resp.status_code == 403:
        reason = _FORBIDDEN_FLEET if fleet_id else _FORBIDDEN
        return None, {"available": False, "reason": reason}, fleet_id
    if resp.status_code != 200:
        return None, {"available": False, "reason": _UNREACHABLE}, fleet_id
    try:
        body = resp.json()
    except ValueError:
        return None, {"available": False, "reason": _UNREACHABLE}, fleet_id
    if not isinstance(body, dict):
        return None, {"available": False, "reason": _UNREACHABLE}, fleet_id
    return body, None, fleet_id


def _vehicle(row: dict) -> dict[str, Any]:
    return {
        "vehicleId": row.get("vehicleId"),
        "vin": row.get("vin"),
        "make": row.get("make"),
        "model": row.get("model"),
        "year": row.get("year"),
    }


def make_fleet_lifecycle_tool(ctx: Any, config: Any) -> Any:
    """Factory: fleet_lifecycle bound to the session."""

    @tool
    def fleet_lifecycle() -> dict[str, Any]:
        """Fleet replacement and tire picture from Fleet Intelligence: how many
        vehicles are recommended to sell or sell soon, how many are healthy, how
        many need new tires, and which vehicles are closest to their crossover
        month. Covers the fleet on the user's screen, or every fleet the user can
        see. Call this for questions like "which vehicles should we replace",
        "what should we sell", "how many need tires", or "how is the fleet doing".

        Crossover is the month when a vehicle's projected monthly maintenance cost
        exceeds its monthly straight-line depreciation. Use `summary` for counts.
        `soonestCrossover` lists vehicles with a reliable cost trend, soonest first.
        Say the figures are projections; the decision to sell is the fleet
        manager's. If `provenance` is "simulated", say the figures come from
        simulated data. Give `computedAt` as "as of". If `available` is false, say
        the reason.

        Returns:
            available, fleetId (null means all visible fleets), summary,
            soonestCrossover, tiresNeedReplacement, provenance, computedAt.
        """
        body, unavailable, fleet_id = _get(ctx, config, _LIFECYCLE_PATH, "fleet_lifecycle")
        if unavailable:
            return unavailable
        rows = [r for r in (body.get("rows") or []) if isinstance(r, dict)]
        summary = body.get("summary") if isinstance(body.get("summary"), dict) else {}
        # No vehicles means no data, not a healthy fleet (security review S1).
        if not rows or not summary.get("totalVehicles"):
            return {"available": False, "reason": _NO_LIFECYCLE_DATA}
        timed = sorted(
            (r for r in rows if r.get("rSquaredMeaningful") and isinstance(r.get("monthsUntilCrossover"), (int, float))),
            key=lambda r: r["monthsUntilCrossover"],
        )
        soonest = [
            {
                **_vehicle(r),
                "monthsUntilCrossover": r.get("monthsUntilCrossover"),
                "currentMonthlyMaintenance": _round(r.get("currentMonthlyMaintenance")),
                "monthlyDepreciation": _round(r.get("monthlyDepreciation")),
            }
            for r in timed[:_MAX_VEHICLES]
        ]
        tires = [
            {**_vehicle(r), "tirePositions": r.get("tirePositions")}
            for r in rows if r.get("tireStatus") == "needs_replacement"
        ][:_MAX_VEHICLES]
        return {
            "available": True,
            "fleetId": fleet_id,
            "summary": {k: _round(v) for k, v in summary.items()},
            "soonestCrossover": soonest,
            "tiresNeedReplacement": tires,
            "provenance": body.get("provenance"),
            "computedAt": body.get("computedAt"),
        }

    return fleet_lifecycle


def make_fleet_cost_outliers_tool(ctx: Any, config: Any) -> Any:
    """Factory: fleet_cost_outliers bound to the session."""

    @tool
    def fleet_cost_outliers() -> dict[str, Any]:
        """Vehicles whose cost per mile is at or above the configured threshold
        against both their own recent average and the median of comparable vehicles
        that month, from ADP's cost baseline — or the same rule on energy cost.
        Covers the fleet on the user's screen, or every fleet the user can see.
        Call this for "which vehicles cost the most", "cost outliers", or "where
        is maintenance spend going".

        Each outlier carries `flag` ("total", "energy", or "both"), `severity` (derived
        from the total cost-per-mile ratio to the comparable-vehicle median, whatever the
        flag: "attention", "soon", or "routine"), and `ratioToComparable` (the energy
        cohort ratio when `flag == "energy"`, the total cost cohort ratio otherwise;
        rounded to 2 decimal places; None when unavailable).

        An empty `outliers` list means no vehicle meets the threshold. If
        `provenance` is "simulated", say the figures come from simulated data.
        Give `computedAt` as "as of". If `available` is false, say the reason.

        Returns:
            available, fleetId, fleetMeanCostPerMile, outliers, provenance, computedAt.
        """
        body, unavailable, fleet_id = _get(ctx, config, _OUTLIERS_PATH, "fleet_cost_outliers")
        if unavailable:
            return unavailable
        data = [r for r in (body.get("data") or []) if isinstance(r, dict)]
        # A zero or missing fleet mean means no cost rows were read; an empty
        # outlier list would then falsely read as "no outliers" (security review S1).
        mean = body.get("fleetMeanCpm")
        if not isinstance(mean, (int, float)) or mean <= 0:
            return {"available": False, "reason": _NO_COST_DATA}
        data.sort(key=lambda r: r.get("cpm") or 0, reverse=True)
        return {
            "available": True,
            "fleetId": fleet_id,
            "fleetMeanCostPerMile": _round(body.get("fleetMeanCpm")),
            "outliers": [
                {
                    "vehicleId": r.get("group"),
                    "costPerMile": _round(r.get("cpm")),
                    "totalCost": _round(r.get("totalCost")),
                    "totalMiles": _round(r.get("totalMiles"), 0),
                    "flag": r.get("flag"),
                    "severity": r.get("severity"),
                    # ratioToComparable: use energy cohort ratio when the vehicle is
                    # flagged for energy cost, otherwise use the total-cost cohort ratio.
                    # Matches the UI picker (issues/2026-10-09-fi-cost-screen-defects/).
                    "ratioToComparable": _round(
                        r.get("ratioCohortEnergy")
                        if r.get("flag") == "energy"
                        else r.get("ratioCohort")
                    ),
                }
                for r in data[:_MAX_VEHICLES]
            ],
            "provenance": body.get("provenance"),
            "computedAt": body.get("computedAt"),
        }

    return fleet_cost_outliers
