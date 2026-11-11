"""Pin: every FI component maps to an agent-fi- id; predicates agree (call 57).

Spec: .kiro/specs/2026-10-05-fi-recall-warranty-stage-c/decisions.md call 57

Assertions:
  A — map keys == VALID_COMPONENTS
  B — every component outside {battery, tires, brakes, diagnostics} maps to agent-fi-*
  C — fi_cost AGENT_ID == map["cost"]
  D — fi_recall_warranty AGENT_ID == map["recall"] == map["warranty"]
  E — runner constants equal the map
  F — both _is_fi_finding predicates: True for every FI id (no recommended_action),
      False for agent-2-vehicle-health

Mutants caught (see /tmp/2026-10-05-fi-recall-warranty-stage-c-g4-s6/note.md):
  M1  fleet_health added with id agent-5-fleet-health  → B fails
  M2  VALID_COMPONENTS extended but map not             → A fails
  M3  broker prefix changed to agent-fx-               → F fails on broker check
"""
from __future__ import annotations

import os
import sys

import pytest

from agents.tier2.contract import AGENT_ID_BY_COMPONENT, VALID_COMPONENTS
from agents.fi_cost.finding_keys import AGENT_ID as FI_COST_AGENT_ID
from agents.fi_recall_warranty.writer import AGENT_ID as FI_RW_AGENT_ID
from agents.tier2.runner import (
    FI_COST_AGENT_ID as RUNNER_FI_COST_ID,
    FI_RECALL_WARRANTY_AGENT_ID as RUNNER_FI_RW_ID,
    HEALTH_AGENT_ID as RUNNER_HEALTH_ID,
)

# ---------------------------------------------------------------------------
# Path setup — broker (independent bundle; no cross-import with findings handler)
# ---------------------------------------------------------------------------
_BROKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                            "..", "..", "supervisor", "lambdas"))
if _BROKER_DIR not in sys.path:
    sys.path.insert(0, _BROKER_DIR)

import avx_broker as _broker  # noqa: E402

# ---------------------------------------------------------------------------
# Path setup — findings handler (independent bundle)
# ---------------------------------------------------------------------------
_FINDINGS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                              "..", "..", "..", "lambdas", "api-avx-findings"))
if _FINDINGS_DIR not in sys.path:
    sys.path.insert(0, _FINDINGS_DIR)

import handler as _handler  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_HEALTH_COMPONENTS = frozenset({"battery", "tires", "brakes", "diagnostics"})


# ---------------------------------------------------------------------------
# A — map keys == VALID_COMPONENTS
# ---------------------------------------------------------------------------
def test_map_keys_equal_valid_components() -> None:
    assert set(AGENT_ID_BY_COMPONENT.keys()) == VALID_COMPONENTS


# ---------------------------------------------------------------------------
# B — non-health components map to agent-fi- prefix
# ---------------------------------------------------------------------------
def test_non_health_components_have_fi_prefix() -> None:
    fi_components = VALID_COMPONENTS - _HEALTH_COMPONENTS
    assert fi_components, "expected at least one FI component"
    for comp in fi_components:
        agent_id = AGENT_ID_BY_COMPONENT[comp]
        assert agent_id.startswith("agent-fi-"), (
            f"component {comp!r} maps to {agent_id!r}; expected agent-fi- prefix"
        )


# ---------------------------------------------------------------------------
# C — fi_cost package AGENT_ID == map["cost"]
# ---------------------------------------------------------------------------
def test_fi_cost_agent_id_equals_map() -> None:
    assert FI_COST_AGENT_ID == AGENT_ID_BY_COMPONENT["cost"]


# ---------------------------------------------------------------------------
# D — fi_recall_warranty AGENT_ID == map["recall"] == map["warranty"]
# ---------------------------------------------------------------------------
def test_fi_rw_agent_id_equals_map_recall_and_warranty() -> None:
    assert FI_RW_AGENT_ID == AGENT_ID_BY_COMPONENT["recall"]
    assert FI_RW_AGENT_ID == AGENT_ID_BY_COMPONENT["warranty"]


# ---------------------------------------------------------------------------
# E — runner constants equal the map
# ---------------------------------------------------------------------------
def test_runner_constants_equal_map() -> None:
    assert RUNNER_HEALTH_ID == AGENT_ID_BY_COMPONENT["battery"]
    assert RUNNER_FI_COST_ID == AGENT_ID_BY_COMPONENT["cost"]
    assert RUNNER_FI_RW_ID == AGENT_ID_BY_COMPONENT["recall"]


# ---------------------------------------------------------------------------
# F — _is_fi_finding: True for every FI agent id, False for health
# ---------------------------------------------------------------------------
# .get(): a component missing from the map must fail test_map_keys_equal_valid_components on
# its own assertion, not abort collection with a KeyError (architect fix, Group 4).
_FI_AGENT_IDS = sorted({AGENT_ID_BY_COMPONENT.get(c, f"<unmapped:{c}>")
                        for c in VALID_COMPONENTS - _HEALTH_COMPONENTS})
_FI_AGENT_IDS_DEDUPED = list(dict.fromkeys(_FI_AGENT_IDS))  # preserve order, remove dups


@pytest.mark.parametrize("agent_id", _FI_AGENT_IDS_DEDUPED)
def test_is_fi_finding_true_for_fi_agent_handler(agent_id: str) -> None:
    finding = {"agent_id": agent_id}
    assert _handler._is_fi_finding(finding), (
        f"handler._is_fi_finding returned False for agent_id={agent_id!r}"
    )


@pytest.mark.parametrize("agent_id", _FI_AGENT_IDS_DEDUPED)
def test_is_fi_finding_true_for_fi_agent_broker(agent_id: str) -> None:
    finding = {"agent_id": agent_id}
    assert _broker._is_fi_finding(finding), (
        f"broker._is_fi_finding returned False for agent_id={agent_id!r}"
    )


def test_is_fi_finding_false_for_health_handler() -> None:
    finding = {"agent_id": AGENT_ID_BY_COMPONENT["battery"]}
    assert not _handler._is_fi_finding(finding)


def test_is_fi_finding_false_for_health_broker() -> None:
    finding = {"agent_id": AGENT_ID_BY_COMPONENT["battery"]}
    assert not _broker._is_fi_finding(finding)
