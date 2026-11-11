"""
Finding-key helpers for the fi_cost Tier 2 component.

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D8

D8: ``subject_key`` = the data month (``YYYY-MM``) for both ``fleet.cost.anomaly``
and ``fleet.cost.sell_timing``.  A re-run on the same month updates the existing
item in place; next month's recommendation is a new queue item.

``finding_key`` delegates to ``agents.tier2.writer.compute_finding_key`` with
``subject_key=year_month``, which adds the month to the canonical SHA-256 payload.
Two months for one VIN give two distinct ``finding_key``s; the same month twice
gives one (the D8 invariant).
"""

from __future__ import annotations

from agents.tier2.writer import compute_finding_key

#: Agent identifier for all fi_cost Findings.
AGENT_ID: str = "agent-fi-cost"

#: Both cost Finding kinds use VIN scope.
SCOPE_FIELD: str = "vin"

#: Recognised finding kinds for this component.
FINDING_KIND_ANOMALY: str = "fleet.cost.anomaly"
FINDING_KIND_SELL_TIMING: str = "fleet.cost.sell_timing"


def cost_finding_key(
    vin: str,
    finding_kind: str,
    year_month: str,
) -> str:
    """Compute the ``finding_key`` for a cost Finding.

    Uses ``subject_key=year_month`` so that two months for one VIN produce two
    distinct keys (D8: one item per vehicle per data month) and the same month
    twice produces one (idempotent in-place update).

    Parameters
    ----------
    vin:
        Vehicle VIN string (scope value).
    finding_kind:
        ``"fleet.cost.anomaly"`` or ``"fleet.cost.sell_timing"``.
    year_month:
        Data month in ``"YYYY-MM"`` format.  This is the *data* month, not the
        wall-clock run date.

    Returns
    -------
    str
        64-character lowercase hex SHA-256 digest.
    """
    return compute_finding_key(
        agent_id=AGENT_ID,
        scope_field=SCOPE_FIELD,
        scope_value=vin,
        finding_kind=finding_kind,
        subject_key=year_month,
    )
