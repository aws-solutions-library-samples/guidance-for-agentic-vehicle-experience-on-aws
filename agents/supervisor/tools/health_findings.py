"""health_findings tool — Tier 1 read surface for Tier 2 platform health artifacts.

Spec: `.kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md` § D9.

Design invariants (enforced by test_health_findings.py):
- **No LLM in the path.** This is a fast DynamoDB read only.
- **No Athena call.** The Tier 2 agent already ran those; we read the artifact.
- Single-partition ``Query`` on ``pk = VEH#{vin}``.
- ``begins_with(sk, "FINDING#{component}#")`` to enumerate all content-addressed
  items for a component (sk is ``FINDING#{component}#{inputs_hash}``; the sort key
  is content-addressed, not time-ordered — spec § D4 corrected 2026-08-04).
- Newest finding per component is selected by ``max(computed_at)`` in the caller,
  not by a descending sort — the sort key carries ``inputs_hash``, not a timestamp.
- Returns ``computed_at`` (M5) so staleness is surfaced to the caller.
- ``evidence[]`` returned only when ``include_evidence=True`` to keep the default
  response lean; the field is always present in the artifact, just gate-kept here.

Table name and region come from environment variables set by the CDK stack:
    HEALTH_FINDINGS_TABLE  (e.g. ``vsa-staging-health-findings``)
    DYNAMODB_REGION        (e.g. ``us-west-2``)

If the env vars are absent (local dev, test harness without real infrastructure),
safe defaults fall back gracefully without masking the error to callers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from strands import tool

log = logging.getLogger(__name__)

# Components defined by the artifact contract (agents/tier2/contract.py).
# Duplicated here to avoid importing tier2 code inside the supervisor package —
# the dependency direction is supervisor → artifact *table*, not supervisor → tier2.
_VALID_COMPONENTS = frozenset({"tires", "battery", "brakes"})

_DEFAULT_TABLE = "vsa-staging-health-findings"
_DEFAULT_REGION = "us-west-2"


def make_health_findings_tool(session: Any, config: Any) -> Any:
    """Factory returning a @tool that reads Tier 2 health-finding artifacts from DynamoDB.

    The tool is a fast, LLM-free DynamoDB read.  It queries the single partition
    for the VIN in session, finds the newest artifact per requested component(s),
    and returns the finding text together with ``computed_at`` so the agent can
    surface staleness ("as of last Tuesday") to the owner.

    Registered on: fleet_driver, vehicle_owner, consumer, service_advisor.
    NOT registered on the anonymous kiosk — a kiosk visitor has no vehicle.

    Args:
        session: SessionContext with ``vin`` attribute.
        config:  PersonaLoaderConfig (not used directly; accepted for signature
                 consistency with all other tool factories).
    """
    # Resolve table name and region at factory time — the env vars are set once
    # per process by the CDK stack and do not change between calls.
    table_name = os.environ.get("HEALTH_FINDINGS_TABLE", _DEFAULT_TABLE)
    dynamodb_region = os.environ.get("DYNAMODB_REGION", _DEFAULT_REGION)

    @tool
    def get_health_findings(
        component: str | None = None,
        include_evidence: bool = False,
    ) -> dict[str, Any]:
        """Retrieve the latest Tier 2 platform health findings for the vehicle in session.

        Returns pre-computed health findings (tires, battery, brakes) produced by
        the Tier 2 platform health agent.  Each finding includes the component name,
        a plain-language finding text, a scheduling-urgency hint, a confidence level,
        and the ``computed_at`` timestamp so you can tell the owner how fresh the
        assessment is (e.g. "as of last Tuesday").

        This is a fast read — no live sensor query, no ADP access, no LLM call.

        Args:
            component: One of ``tires``, ``battery``, ``brakes``, or ``None`` to
                       return findings for all three components.
            include_evidence: If ``True``, include the raw evidence list attached to
                              each finding.  Default ``False`` to keep the response
                              lean for conversational use.

        Returns a dict with key ``findings`` (list of finding objects) and
        ``available`` (bool).  Each finding carries ``component``, ``finding``,
        ``computed_at``, ``confidence``, ``severity_hint``, and optionally
        ``evidence``.
        """
        if not session.vin:
            return {"available": False, "reason": "No VIN in session context"}

        # Validate and resolve the component filter.
        if component is not None:
            comp_lower = component.lower()
            if comp_lower not in _VALID_COMPONENTS:
                return {
                    "available": False,
                    "reason": (
                        f"Unknown component {component!r}. "
                        f"Valid values: {sorted(_VALID_COMPONENTS)}"
                    ),
                }
            components_to_fetch = [comp_lower]
        else:
            components_to_fetch = sorted(_VALID_COMPONENTS)

        pk = f"VEH#{session.vin}"

        try:
            dynamodb = boto3.resource("dynamodb", region_name=dynamodb_region)
            table = dynamodb.Table(table_name)
        except Exception as exc:
            log.warning("health_findings: DynamoDB client init failed: %s", exc)
            return {"available": False, "reason": "health findings service unavailable"}

        findings: list[dict[str, Any]] = []

        for comp in components_to_fetch:
            sk_prefix = f"FINDING#{comp}#"
            try:
                resp = table.query(
                    KeyConditionExpression=(
                        Key("pk").eq(pk)
                        & Key("sk").begins_with(sk_prefix)
                    ),
                    # Project only the fields we return — avoids reading large
                    # evidence lists when include_evidence=False.
                    ProjectionExpression=(
                        "computed_at, finding, confidence, severity_hint, evidence"
                        if include_evidence
                        else "computed_at, finding, confidence, severity_hint"
                    ),
                )
            except Exception as exc:
                log.warning(
                    "health_findings: Query failed for component=%s pk=%s: %s",
                    comp,
                    pk,
                    exc,
                )
                continue

            items = resp.get("Items", [])
            if not items:
                # No finding for this component — not an error, just absent.
                continue

            # The sort key is content-addressed (inputs_hash), not time-ordered.
            # Select the item with the most recent computed_at.
            newest = max(items, key=lambda it: it.get("computed_at", ""))

            entry: dict[str, Any] = {
                "component": comp,
                "finding": newest.get("finding", ""),
                "computed_at": newest.get("computed_at", ""),
                "confidence": newest.get("confidence", ""),
                "severity_hint": newest.get("severity_hint", ""),
            }
            if include_evidence:
                entry["evidence"] = newest.get("evidence", [])

            findings.append(entry)

        if not findings:
            return {
                "available": False,
                "reason": (
                    "No health findings available yet. "
                    "The Tier 2 health agent runs on a daily schedule."
                ),
            }

        return {"available": True, "findings": findings}

    return get_health_findings
