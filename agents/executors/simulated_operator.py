"""The simulated fleet operator's identity and scope (spec D10, prompt 276).

A scheduled, deterministic stand-in (no model) that approves FI items for the four
staging demo fleets. It is not an auto-approval rule: it records its own decider kind,
``simulated_fleet_operator``, and the guardrails admit that kind only inside the
stand-in's own process (``FI_SIM_OPERATOR_ENABLED``) and only for these fleets.

``DEMO_FLEET_IDS`` is a literal, pinned by name and never read from configuration, so a
configuration change can't widen it. CMS's internal groundings route and DMS's internal
routes keep their own copy of the same four ids (cross-repo pin tests).
"""
from __future__ import annotations

import os
from typing import Any, Mapping

#: ``decided_by.kind`` for a decision the stand-in made. CMS writes the same string as
#: ``returnedByKind`` / ``groundedByKind``, so one term names the actor everywhere.
DECIDER_KIND = "simulated_fleet_operator"

#: ``decided_by.id`` and ``created_by`` for every stand-in decision (never a person's sub).
DECIDER_ID = "simulated-fleet-operator"

#: The only fleets the stand-in may ever act on (spec D10 Decision 3).
DEMO_FLEET_IDS: frozenset[str] = frozenset({
    "demo-lastmile-delivery",
    "demo-field-service",
    "demo-city-public",
    "demo-rental-carshare",
})

#: Set to exactly ``"true"`` only on the stand-in's own Lambda. Any other process (the
#: approve route, the auto-approver, an executor Lambda) refuses a simulated decider.
ENABLED_ENV = "FI_SIM_OPERATOR_ENABLED"

#: The kinds the stand-in may decide (spec D10b). Everything else is never its decision.
SIMULATED_KINDS: frozenset[str] = frozenset({
    "ground_vehicle",
    "book_fleet_service",
    "book_recall_service",
    "submit_warranty_claim",
})


def process_enabled() -> bool:
    """True only when this process is the stand-in (``FI_SIM_OPERATOR_ENABLED == "true"``)."""
    return os.environ.get(ENABLED_ENV) == "true"


def decided_by() -> dict[str, str]:
    """A fresh ``decided_by`` for a stand-in decision."""
    return {"kind": DECIDER_KIND, "id": DECIDER_ID}


def is_simulated_decider(value: Any) -> bool:
    """True iff ``value`` is exactly the stand-in's ``decided_by`` (kind and id)."""
    return (isinstance(value, Mapping)
            and value.get("kind") == DECIDER_KIND
            and value.get("id") == DECIDER_ID)
