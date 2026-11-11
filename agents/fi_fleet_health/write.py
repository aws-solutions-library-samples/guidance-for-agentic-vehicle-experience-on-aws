"""Finding-level writer for the FI fleet-health agent (spec D9, contract § 1).

This module owns the AGENT_ID constant and functions that produce complete
Finding-shaped dicts for storage via agents/tier2/writer.write_finding().

Public API
----------
AGENT_ID       : str — "agent-fi-fleet-health"
AGENT_VERSION  : str — semver pinned per artifact write
FINDING_KIND   : str — "fleet.health.service"

build_finding(vin, fleet_id, item, computed_at, inputs_hash, evidence,
              confidence) -> dict
    Build a full Finding dict from an item dict returned by writer.build_all_items().

build_finding_no_fleet(vin) -> dict
    Build a minimal Finding with scope.vin only (no fleet_id), for vehicles
    with no resolved fleet.  Contract § 7: a vehicle with no resolved fleet
    is in no queue (fail closed).  Used by the invariant test.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from agents.tier2.writer import compute_finding_key

#: Agent identifier — broker skip and iOS exclusion rely on the "agent-fi-" prefix.
AGENT_ID: str = "agent-fi-fleet-health"

#: Semver pinned per artifact write.
AGENT_VERSION: str = "0.1.0"

#: Finding kind for fleet-health booking and grounding items.
FINDING_KIND: str = "fleet.health.service"

#: TTL in days — refreshed by each daily run (contract § 1.8).
_EXPIRY_DAYS: int = 3


def _iso_add_days(iso: str, days: int) -> str:
    """Add *days* to an ISO-8601 UTC string and return the result."""
    import datetime
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    result = dt + datetime.timedelta(days=days)
    return result.isoformat().replace("+00:00", "Z")


def build_finding(
    *,
    vin: str,
    fleet_id: Optional[str],
    item: dict,
    computed_at: str,
    inputs_hash: str,
    evidence: list,
    confidence: str,
) -> dict:
    """Build a full Finding dict.

    ``fleet_id`` is projected from the vehicle's fleet membership by the runner.
    When ``fleet_id`` is None (vehicle has no resolved fleet), the Finding carries
    only ``scope.vin`` and is in no fleet queue (contract § 7 fail-closed).

    Parameters
    ----------
    vin:          Vehicle VIN.
    fleet_id:     Fleet id or None.
    item:         Item dict from writer.build_all_items().
    computed_at:  ISO-8601 UTC string.
    inputs_hash:  64-char SHA-256 hex of the input bundle.
    evidence:     Evidence list.
    confidence:   "low" | "medium" | "high".

    Returns
    -------
    dict
        Finding matching agents/tier2/schemas/finding.schema.json.
    """
    subject_key = item.get("subject_key", "")
    severity = item.get("severity")
    if severity is None:
        raise ValueError(
            f"build_finding: item missing 'severity' (subject_key={subject_key!r}). "
            "Every fleet-health item must carry an explicit severity band."
        )

    finding_key = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=FINDING_KIND,
        subject_key=subject_key or None,
    )

    finding: dict[str, Any] = {
        "finding_id": f"FIND-{uuid.uuid4()}",
        "finding_key": finding_key,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "scope": {"vin": vin},
        "severity": severity,
        "computed_at": computed_at,
        "expires_at": _iso_add_days(computed_at, _EXPIRY_DAYS),
        "confidence": confidence,
        "finding_kind": FINDING_KIND,
        "headline": item.get("headline", ""),
        "detail": item.get("detail", ""),
        "evidence": evidence,
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        "subject_key": subject_key,
    }

    # Forward optional fields
    disclosure = item.get("disclosure_txt")
    if disclosure is not None:
        finding["disclosure_txt"] = disclosure

    recommended_action = item.get("recommended_action")
    if recommended_action is not None:
        finding["recommended_action"] = recommended_action

    return finding


def build_finding_no_fleet(vin: str) -> dict:
    """Build a minimal fleet-health Finding with no fleet_id.

    Used when the vehicle's fleet cannot be resolved.  The Finding carries
    ``scope.vin`` only — no ``fleet_id`` at any level.  Contract § 7:
    a vehicle with no resolved fleet is in no fleet queue (fail closed).

    Mutation guard (test_fleet_health_finding_population_scoped_has_no_fleet_id):
    any dict containing ``fleet_id`` fails the assertion.
    """
    return {
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "finding_kind": FINDING_KIND,
        "scope": {"vin": vin},
        # No fleet_id at top level or inside scope.
    }
