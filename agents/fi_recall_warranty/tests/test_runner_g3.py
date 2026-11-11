"""Additional runner tests for task 3.1D — mutation anchors.

Mutation (e): changing p0 → 'this_month' in BandResult.urgency or in
build_rules_plan must be caught here because the main test only checks
equality (plan['urgency'] == band_result.urgency), which would pass if
BOTH were changed to 'this_month'. This test pins the literal value.
"""
from __future__ import annotations

import pytest


class TestApplyCapSortBeforeCap:
    """Mutation-C anchor: sort must happen BEFORE cap, not after.

    The main test (test_apply_cap_uses_highest_severity_first_within_type) places
    p0 items first in the input, so cap-before-sort gives the same result as
    sort-before-cap.  This fixture puts routine items FIRST to expose that mutation.
    """

    def test_routine_first_in_input_p0_must_survive_cap(self):
        """When routine items precede p0 items in input, only p0 must survive cap=3.

        M-C mutation: cap-before-sort keeps the first 3 routine items and drops p0.
        """
        from agents.fi_recall_warranty.runner import apply_cap

        KIND = "fleet.recall.vehicle"
        # routine items are FIRST — cap-before-sort would keep them
        # 3 routine + 3 p0, cap=3: sort-first gives all p0; cap-first gives all routine
        recalls = (
            [{"finding_kind": KIND, "severity": "routine"}] * 3
            + [{"finding_kind": KIND, "severity": "p0"}] * 3
        )
        result = apply_cap(recalls, recall_cap=3, warranty_cap=30)
        surviving = [r for r in result if r["finding_kind"] == KIND]
        assert len(surviving) == 3, f"Expected 3 survivors after cap=3, got {len(surviving)}."
        assert all(r["severity"] == "p0" for r in surviving), (
            f"All 3 survivors must be p0; got {[r['severity'] for r in surviving]}. "
            "M-C: cap-before-sort keeps the first 3 routine items (input order) and drops p0."
        )


class TestP0UrgencyLiteral:
    """Pin the literal urgency value for p0 (mutation-e anchor)."""

    def test_p0_band_urgency_is_this_week(self):
        """BandResult.urgency for p0 must literally be 'this_week'.

        M-E mutation: returning 'this_month' for p0 must fail here.
        """
        from agents.fi_recall_warranty import band

        result = band.classify("safety", True, "CONFIRMED")  # → p0
        assert result.severity == "p0"
        assert result.urgency == "this_week", (
            f"p0 severity must map to urgency='this_week', got {result.urgency!r}. "
            "M-E mutation: changing p0 → 'this_month' breaks this assertion."
        )

    def test_attention_urgency_is_this_month(self):
        """BandResult.urgency for attention must be 'this_month'."""
        from agents.fi_recall_warranty import band

        result = band.classify("safety", False, "POPULATION")  # → attention
        assert result.severity == "attention"
        assert result.urgency == "this_month", (
            f"attention severity must map to urgency='this_month', got {result.urgency!r}."
        )

    def test_soon_urgency_is_next_visit(self):
        """BandResult.urgency for soon must be 'next_visit'."""
        from agents.fi_recall_warranty import band

        result = band.classify("non_safety", False, "CONFIRMED")  # → soon
        assert result.severity == "soon"
        assert result.urgency == "next_visit", (
            f"soon severity must map to urgency='next_visit', got {result.urgency!r}."
        )

    def test_routine_urgency_is_next_visit(self):
        """BandResult.urgency for routine must be 'next_visit'."""
        from agents.fi_recall_warranty import band

        result = band.classify("non_safety", False, "POPULATION")  # → routine
        assert result.severity == "routine"
        assert result.urgency == "next_visit", (
            f"routine severity must map to urgency='next_visit', got {result.urgency!r}."
        )

    def test_build_rules_plan_p0_urgency_literal(self):
        """build_rules_plan for p0 band must produce urgency='this_week' literally.

        Catches the M-E mutation on runner.py (build_rules_plan uses 'this_month' for p0).
        """
        from agents.fi_recall_warranty import band, runner

        band_result = band.classify("safety", True, "CONFIRMED")  # p0
        candidates = [{"dealer_id": "dealer-a", "distance_miles": 5.0}]
        plan = runner.build_rules_plan(candidates, band_result)

        assert plan["urgency"] == "this_week", (
            f"build_rules_plan for p0 must set urgency='this_week', got {plan['urgency']!r}. "
            "M-E mutation: hardcoding 'this_month' for p0 in build_rules_plan fails here."
        )
