# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the oracle-fix functions in live-verify-findings-me.py (spec A2).

The six live-check failures (decisions.md 2026-10-27 ~14:15Z) came from three oracle bugs:
  1. FI items (agent_id starts with "agent-fi-", or has recommended_action) counted in
     the expected set; the route hides them (spec D13).
  2. Cursor checks used only the live (unexpired) partition; the route may name expired
     rows as cursors because it reads the full partition.
  3. Fleet check required truncated=False even when a fleet has >2,000 rows and the
     route intentionally stops at that limit.

These tests verify the helper functions extracted to fix those bugs.

Commit history:
  RED   — tests fail because _is_fi_item / _build_live_non_fi_by /
          _build_whole_by / _is_fleet_over_limit don't exist in the script yet
  GREEN — functions added; tests pass
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

_CVX_ROOT = Path(__file__).resolve().parents[2]
if str(_CVX_ROOT) not in sys.path:
    sys.path.insert(0, str(_CVX_ROOT))

# Import live-verify-findings-me.py (has hyphens; can't use regular import).
_mod_path = _CVX_ROOT / "scripts" / "live-verify-findings-me.py"
_spec = importlib.util.spec_from_file_location("live_verify_findings_me", str(_mod_path))
lvm: types.ModuleType = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(lvm)  # type: ignore[union-attr]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(finding_id: str, *, vehicle_id: str = "VEH-001", owner_id: str = "OWNER-001",
         fleet_id: str = "", vin: str = "MRDN0000001000001", agent_id: str = "agent-2-vehicle-health",
         expires_at: str = "2099-01-01T00:00:00Z",
         recommended_action: dict | None = None) -> dict:
    r: dict = {
        "finding_id": finding_id,
        "vehicle_id": vehicle_id,
        "owner_id": owner_id,
        "vin": vin,
        "agent_id": agent_id,
        "expires_at": expires_at,
    }
    if fleet_id:
        r["fleet_id"] = fleet_id
    if recommended_action is not None:
        r["recommended_action"] = recommended_action
    return r


NOW_STR = "2026-10-27T12:00:00Z"
PAST = "2026-10-01T00:00:00Z"    # expired
FUTURE = "2099-01-01T00:00:00Z"  # unexpired


# ── _is_fi_item ───────────────────────────────────────────────────────────────

class TestIsFiItem:
    """Tests for lvm._is_fi_item(row)."""

    def test_recommended_action_present_is_fi(self) -> None:
        assert lvm._is_fi_item({"agent_id": "agent-2-vehicle-health",
                                 "recommended_action": {"action": "book"}}) is True

    def test_agent_fi_recall_warranty_is_fi(self) -> None:
        assert lvm._is_fi_item({"agent_id": "agent-fi-recall-warranty"}) is True

    def test_agent_fi_quality_is_fi(self) -> None:
        assert lvm._is_fi_item({"agent_id": "agent-fi-quality"}) is True

    def test_agent_fi_cost_is_fi(self) -> None:
        assert lvm._is_fi_item({"agent_id": "agent-fi-cost"}) is True

    def test_agent_fi_fleet_health_is_fi(self) -> None:
        assert lvm._is_fi_item({"agent_id": "agent-fi-fleet-health"}) is True

    def test_ordinary_health_row_is_not_fi(self) -> None:
        assert lvm._is_fi_item({"agent_id": "agent-2-vehicle-health",
                                  "finding_kind": "health.brakes"}) is False

    def test_empty_row_is_not_fi(self) -> None:
        assert lvm._is_fi_item({}) is False

    def test_no_agent_no_recommended_action_is_not_fi(self) -> None:
        assert lvm._is_fi_item({"finding_id": "FIND-X"}) is False

    def test_recommended_action_none_value_is_fi(self) -> None:
        """A key-present-but-None value still makes the row an FI item.

        The implementation uses ``in`` (presence check) not truthiness, matching
        DynamoDB's attribute_exists semantics.  This test pins that: reverting
        to ``row.get("recommended_action")`` (truthiness) makes None falsy and
        the test fails.
        """
        assert lvm._is_fi_item({"recommended_action": None}) is True


# ── _build_live_non_fi_by ─────────────────────────────────────────────────────

class TestBuildLiveNonFiBy:
    """Tests for lvm._build_live_non_fi_by(all_items, now_str).

    Returns a dict-of-sets {attribute → {value → set(finding_id)}} containing only
    rows that are (a) unexpired and (b) not FI items.
    """

    def test_live_non_fi_row_included(self) -> None:
        items = [_row("FIND-OK", expires_at=FUTURE)]
        by = lvm._build_live_non_fi_by(items, NOW_STR)
        assert "FIND-OK" in by["vehicle_id"]["VEH-001"]

    def test_expired_row_excluded(self) -> None:
        items = [_row("FIND-EXP", expires_at=PAST)]
        by = lvm._build_live_non_fi_by(items, NOW_STR)
        assert "FIND-EXP" not in by["vehicle_id"].get("VEH-001", set())

    def test_fi_row_with_recommended_action_excluded(self) -> None:
        items = [_row("FIND-FI", expires_at=FUTURE,
                       recommended_action={"action": "book"})]
        by = lvm._build_live_non_fi_by(items, NOW_STR)
        assert "FIND-FI" not in by["vehicle_id"].get("VEH-001", set())

    def test_fi_agent_row_excluded(self) -> None:
        items = [_row("FIND-RW", expires_at=FUTURE, agent_id="agent-fi-recall-warranty")]
        by = lvm._build_live_non_fi_by(items, NOW_STR)
        assert "FIND-RW" not in by["vehicle_id"].get("VEH-001", set())

    def test_mixed_rows_only_eligible_included(self) -> None:
        items = [
            _row("FIND-OK", expires_at=FUTURE),
            _row("FIND-FI", expires_at=FUTURE, recommended_action={"action": "x"}),
            _row("FIND-EXP", expires_at=PAST),
        ]
        by = lvm._build_live_non_fi_by(items, NOW_STR)
        assert by["vehicle_id"]["VEH-001"] == {"FIND-OK"}


# ── _build_whole_by ───────────────────────────────────────────────────────────

class TestBuildWholeBy:
    """Tests for lvm._build_whole_by(all_items).

    Returns {attribute → {value → set(finding_id)}} including expired rows
    (for cursor-oracle checks).
    """

    def test_expired_row_included_in_whole_by(self) -> None:
        items = [_row("FIND-EXP", expires_at=PAST)]
        by = lvm._build_whole_by(items)
        assert "FIND-EXP" in by["vehicle_id"]["VEH-001"]

    def test_live_row_included_in_whole_by(self) -> None:
        items = [_row("FIND-OK", expires_at=FUTURE)]
        by = lvm._build_whole_by(items)
        assert "FIND-OK" in by["vehicle_id"]["VEH-001"]

    def test_fi_expired_row_included_in_whole_by_for_cursor_check(self) -> None:
        """FI expired rows ARE in whole_by because cursors can reference them."""
        items = [_row("FIND-RW-EXP", expires_at=PAST, agent_id="agent-fi-recall-warranty")]
        by = lvm._build_whole_by(items)
        assert "FIND-RW-EXP" in by["vehicle_id"]["VEH-001"]

    def test_whole_by_contains_more_than_live_non_fi(self) -> None:
        items = [
            _row("FIND-OK", expires_at=FUTURE),
            _row("FIND-EXP", expires_at=PAST),
            _row("FIND-FI", expires_at=FUTURE, recommended_action={"action": "x"}),
        ]
        live_by = lvm._build_live_non_fi_by(items, NOW_STR)
        whole_by = lvm._build_whole_by(items)
        live_ids = live_by["vehicle_id"]["VEH-001"]
        whole_ids = whole_by["vehicle_id"]["VEH-001"]
        assert live_ids < whole_ids  # strict subset


# ── _is_fleet_over_limit ──────────────────────────────────────────────────────

class TestIsFleetOverLimit:
    """Tests for lvm._is_fleet_over_limit(all_items, fleet_id, limit=2000).

    Returns True when the fleet's partition in all_items exceeds limit.
    """

    def test_fleet_with_many_rows_over_limit(self) -> None:
        items = [
            _row(f"FIND-{i}", fleet_id="FLEET-BIG", vehicle_id=f"VEH-{i}")
            for i in range(2001)
        ]
        assert lvm._is_fleet_over_limit(items, "FLEET-BIG", limit=2000) is True

    def test_fleet_with_few_rows_not_over_limit(self) -> None:
        items = [
            _row(f"FIND-{i}", fleet_id="FLEET-SMALL", vehicle_id=f"VEH-{i}")
            for i in range(100)
        ]
        assert lvm._is_fleet_over_limit(items, "FLEET-SMALL", limit=2000) is False

    def test_fleet_exactly_at_limit_is_over(self) -> None:
        """Exactly 2000 rows IS over the limit (route returns truncated:true at >=2000).

        The route's 10th Limit=200 page returns a LastEvaluatedKey when the partition
        has exactly 2000 rows, so the answer says truncated:true.  The oracle must
        accept that by using >= not >.
        """
        items = [
            _row(f"FIND-{i}", fleet_id="FLEET-EXACT", vehicle_id=f"VEH-{i}")
            for i in range(2000)
        ]
        assert lvm._is_fleet_over_limit(items, "FLEET-EXACT", limit=2000) is True

    def test_fleet_at_limit_plus_one_is_over(self) -> None:
        items = [
            _row(f"FIND-{i}", fleet_id="FLEET-OVER", vehicle_id=f"VEH-{i}")
            for i in range(2001)
        ]
        assert lvm._is_fleet_over_limit(items, "FLEET-OVER", limit=2000) is True

    def test_unknown_fleet_not_over_limit(self) -> None:
        items = [_row("FIND-001", fleet_id="OTHER-FLEET")]
        assert lvm._is_fleet_over_limit(items, "NO-SUCH-FLEET", limit=2000) is False
