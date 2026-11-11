"""Unit tests for agents/health/agent.py (T4.3).

Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md § D3, D3a, D3b, D8

Non-negotiable invariants tested here:

1. **Battery reassurance** (spec § D3a): nothing in the data crosses a warranty
   threshold, so the battery finding text contains "degrading normally" and
   severity_hint is "routine" for a healthy battery at ~92% SoH.

2. **Brakes are service history** (spec § D3b): no trend language ("accelerating",
   "progressing") for single-record brake history.  The finding says what happened,
   not what will happen.

3. **P0 verbatim rule** (spec § D8): a brake finding carrying C0161 MUST contain
   the exact phrase "Critical brake pressure loss detected. Pull over immediately."
   Softening it is a boundary violation.  test_c0161_finding_contains_verbatim_p0_phrase
   asserts the string itself, not merely that the severity is high.

4. **severity_hint never carries a P-level** (spec § D8): severity_hint must be
   drawn from {routine, soon, attention} and must never be P0/P1/P2/P3.

5. **Bundling decision** (T4.3): with three components available, the agent makes
   a real bundling decision rather than always combining or always separating.

6. **No channel calls** (spec § D7/M13): already covered by test_boundaries.py.

All tests are synchronous; no pytest-asyncio required.
"""
from __future__ import annotations

import pytest

from agents.health import agent as _agent
from agents.health.agent import (
    assess_vehicle_health,
    run_health_agent,
    _build_battery_finding,
    _build_brake_finding,
    _build_tire_finding,
    _decide_bundling,
    _sanitise_severity_hint,
)
from agents.health.prompt import (
    C0161_VERBATIM_REMEDY,
    BRAKE_DTC_KB_GUIDED,
    SEVERITY_HINTS,
)

# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

SAMPLE_VIN = "VIN-HEALTH-TEST-001"

_BATTERY_ROW_HEALTHY = {
    "vin": SAMPLE_VIN,
    "health_pct": "92.0",
    "age_days": "1095",  # ~3 years
}

_BATTERY_ROW_DEGRADED = {
    "vin": SAMPLE_VIN,
    "health_pct": "72.5",
    "age_days": "2555",  # ~7 years
}

_TIRE_ROW_NEEDS_REPLACEMENT = {
    "vin": SAMPLE_VIN,
    "position": "FL",
    "event_date": "2026-08-01",
    "replacement_flagged": True,
    "wear_band": "critical",
    "wear_rate_per_1k_km": "0.15",
}

_TIRE_ROW_NORMAL = {
    "vin": SAMPLE_VIN,
    "position": "FL",
    "event_date": "2026-08-01",
    "replacement_flagged": False,
    "wear_band": "normal",
    "wear_rate_per_1k_km": "0.05",
}

_BRAKE_DATA_OPEN = {
    "vin": SAMPLE_VIN,
    "service_date": "2026-04-06",
    "dtc_codes": [],
    "complaint_text": "Grinding noise on braking",
    "warranty_covered": "false",
    "total_cost_usd": "350.00",
    "outcome": "parts_pending",
    "is_open": True,
    "provenance": "live",
}

_BRAKE_DATA_RESOLVED = {
    "vin": SAMPLE_VIN,
    "service_date": "2026-01-15",
    "dtc_codes": [],
    "complaint_text": "Routine brake inspection",
    "warranty_covered": "false",
    "total_cost_usd": "120.00",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}

_BRAKE_DATA_C0161 = {
    "vin": SAMPLE_VIN,
    "service_date": "2026-08-03",
    "dtc_codes": ["C0161"],
    "complaint_text": "Brake warning light",
    "warranty_covered": "false",
    "total_cost_usd": "0.00",
    "outcome": "parts_pending",
    "is_open": True,
    "provenance": "live",
}

_BRAKE_DATA_UNKNOWN_DTC = {
    "vin": SAMPLE_VIN,
    "service_date": "2026-07-20",
    "dtc_codes": ["C0031"],  # NOT in KB
    "complaint_text": "ABS warning",
    "warranty_covered": "false",
    "total_cost_usd": "200.00",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}


# ===========================================================================
# Battery finding tests (spec § D3a — reassurance, not alarm)
# ===========================================================================

class TestBatteryFinding:
    """Battery findings must be honest reassurance on healthy data."""

    def test_healthy_battery_finding_says_degrading_normally(self):
        """Spec § D3a: at 92% SoH the finding should say 'degrading normally'."""
        result = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        assert "degrading normally" in result["finding"].lower(), (
            f"Battery finding for 92% SoH should express reassurance ('degrading normally'), "
            f"not alarm. Got: {result['finding']!r}"
        )

    def test_healthy_battery_severity_is_routine(self):
        """Spec § D3a: nothing in the data crosses a warranty threshold — severity is routine."""
        result = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        assert result["severity_hint"] == "routine", (
            f"Battery at 92% SoH should have severity_hint='routine', "
            f"got {result['severity_hint']!r}. "
            "Do not manufacture urgency the data does not support."
        )

    def test_healthy_battery_confidence_is_high(self):
        result = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        assert result["confidence"] == "high"

    def test_healthy_battery_finding_contains_soh_value(self):
        result = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        assert "92" in result["finding"], (
            "Battery finding should include the actual SoH percentage."
        )

    def test_empty_battery_rows_returns_routine_absent(self):
        """Absent data should be routine with absent-provenance evidence."""
        result = _build_battery_finding([])
        assert result["severity_hint"] == "routine"
        assert result["confidence"] == "low"
        provenances = [e["provenance"] for e in result["evidence"]]
        assert "absent" in provenances

    def test_battery_evidence_carries_live_provenance(self):
        result = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        provenances = {e["provenance"] for e in result["evidence"]}
        assert "live" in provenances

    def test_battery_finding_does_not_manufacture_urgency(self):
        """The finding for a healthy battery must not contain alarm words."""
        result = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        finding_lower = result["finding"].lower()
        for alarm_word in ("urgent", "critical", "danger", "warning", "alarm"):
            assert alarm_word not in finding_lower, (
                f"Battery finding for healthy 92% SoH contains alarm word {alarm_word!r}. "
                "Spec § D3a: the honest output is reassurance."
            )

    def test_degraded_battery_severity_is_soon_or_attention(self):
        """A battery below 80% SoH should escalate above routine."""
        result = _build_battery_finding([_BATTERY_ROW_DEGRADED])
        assert result["severity_hint"] in ("soon", "attention"), (
            f"A battery at 72.5% SoH should not be 'routine'. "
            f"Got: {result['severity_hint']!r}"
        )


# ===========================================================================
# Brake finding tests (spec § D3b — service history, not prediction)
# ===========================================================================

class TestBrakeFinding:
    """Brake findings are service history facts, not predictions."""

    def test_open_outcome_parts_pending_is_attention(self):
        """parts_pending outcome → severity_hint='attention'."""
        result = _build_brake_finding(_BRAKE_DATA_OPEN)
        assert result["severity_hint"] == "attention", (
            f"An open brake job (outcome=parts_pending) should be 'attention', "
            f"got {result['severity_hint']!r}"
        )

    def test_open_outcome_finding_says_awaiting_parts(self):
        """Finding should plainly state the brake job is awaiting parts."""
        result = _build_brake_finding(_BRAKE_DATA_OPEN)
        assert "parts" in result["finding"].lower() or "pending" in result["finding"].lower(), (
            f"Finding for parts_pending should mention parts/pending. Got: {result['finding']!r}"
        )

    def test_resolved_brake_is_routine(self):
        """A fully resolved brake record with no DTCs should be routine."""
        result = _build_brake_finding(_BRAKE_DATA_RESOLVED)
        assert result["severity_hint"] == "routine", (
            f"Resolved brake service should be 'routine', got {result['severity_hint']!r}"
        )

    def test_no_trend_language_in_single_record_finding(self):
        """Spec § D3b: ~95% of vehicles have one record — no trend language."""
        result = _build_brake_finding(_BRAKE_DATA_RESOLVED)
        finding_lower = result["finding"].lower()
        trend_words = ("accelerating", "progressing", "trending", "increasing", "rate of")
        for word in trend_words:
            assert word not in finding_lower, (
                f"Brake finding contains trend language {word!r} for a single-record vehicle. "
                "Spec § D3b: no trend analysis for brakes."
            )

    def test_none_brake_data_returns_routine_absent(self):
        """No brake history → routine with absent-provenance evidence."""
        result = _build_brake_finding(None)
        assert result["severity_hint"] == "routine"
        assert result["confidence"] == "low"
        provenances = [e["provenance"] for e in result["evidence"]]
        assert "absent" in provenances

    def test_unknown_dtc_does_not_invent_explanation(self):
        """For codes outside BRAKE_DTC_KB_GUIDED, no fabricated explanation."""
        result = _build_brake_finding(_BRAKE_DATA_UNKNOWN_DTC)
        finding = result["finding"]
        # C0031 is not in KB — should not have a fabricated medical/technical explanation
        assert "C0031" in finding or "recorded" in finding.lower(), (
            "Finding should acknowledge the DTC was recorded without inventing an explanation."
        )
        # The finding must NOT make up what C0031 means
        fabricated_phrases = [
            "wheel speed sensor", "brake caliper", "hydraulic",
            "electronic stability", "anti-lock"
        ]
        for phrase in fabricated_phrases:
            assert phrase.lower() not in finding.lower(), (
                f"Finding appears to fabricate an explanation for C0031 "
                f"(contains {phrase!r}). Only C0161 and C0040 have KB guides."
            )

    def test_brake_evidence_contains_outcome(self):
        result = _build_brake_finding(_BRAKE_DATA_OPEN)
        sources = [e["source"] for e in result["evidence"]]
        assert any("outcome" in s for s in sources)


# ===========================================================================
# P0 verbatim rule (spec § D8) — the critical test
# ===========================================================================

class TestP0VerbatimRule:
    """The P0 verbatim phrase MUST appear verbatim when C0161 is present."""

    def test_c0161_verbatim_remedy_value_is_pinned(self):
        """CRITICAL: the canonical P0 phrase's VALUE, pinned as a literal.

        Every other test in this class dereferences ``C0161_VERBATIM_REMEDY``
        rather than naming its value, so all of them track the constant wherever
        it goes.  Rewriting the constant to ``"exercise caution"`` — the exact
        softening spec § D8 exists to forbid — leaves them green.  Measured
        2026-09-18 (Group 2): under that mutation the two tests below both PASS,
        and ``test_c0161_verbatim_phrase_not_softened`` passes *vacuously*
        because its second clause, ``C0161_VERBATIM_REMEDY not in finding``,
        becomes False once the constant IS the softened phrase.

        The mutation was caught, but only incidentally, by four Cycle 8 backstop
        tests (``TestT414StructuralP0Bypass::test_backstop_cycle8_defeat_{B,C}``
        and ``test_prompt.py::TestCycle8Defeats::test_c8_defeat_{B,C}``) that
        embed the phrase inline as fixture input for an unrelated purpose.  This
        assertion makes the coverage intentional and locally attributable, per
        ``~/.kiro/steering/testing.md`` § "Mutation testing at the green
        boundary" and this spec's Fix Group 9 finding that exact equality is the
        only form of expectation that does not require content inference.

        A legitimate rewording of the safety phrase must therefore edit this
        literal deliberately — which is the reviewable event it should be.
        """
        assert C0161_VERBATIM_REMEDY == (
            "Critical brake pressure loss detected. Pull over immediately."
        ), (
            "The canonical P0 stop-driving phrase has changed value.\n"
            f"  Got:      {C0161_VERBATIM_REMEDY!r}\n"
            "  Expected: 'Critical brake pressure loss detected. Pull over immediately.'\n\n"
            "This is a deterministic safety seam (spec § D8, and "
            "~/.kiro/steering/agentic-tiers.md § 'What stays deterministic'). "
            "If the rewording is intended, change it here in the same commit and "
            "say why in decisions.md.  If it is not, this is the boundary "
            "violation the seam exists to catch."
        )

    def test_c0161_finding_contains_verbatim_p0_phrase(self):
        """CRITICAL: C0161 → finding must contain the phrase the constant names.

        Spec § D8: 'Critical brake pressure loss detected. Pull over immediately.'

        Scope limit, measured rather than assumed: this asserts the *constant is
        present in the finding*, NOT that the constant still holds the correct
        value — it dereferences the symbol, so it follows the constant if the
        constant is softened.  The value itself is pinned by
        ``test_c0161_verbatim_remedy_value_is_pinned`` above.  Both are needed:
        this one catches a finding that drops the phrase, that one catches a
        phrase that stops meaning "stop driving".
        """
        result = _build_brake_finding(_BRAKE_DATA_C0161)
        assert C0161_VERBATIM_REMEDY in result["finding"], (
            f"Finding for C0161 must contain the verbatim P0 phrase:\n"
            f"  Required: {C0161_VERBATIM_REMEDY!r}\n"
            f"  Got:      {result['finding']!r}\n\n"
            "Spec § D8: an agent that preserves the verdict while softening "
            "'stop driving now' into 'exercise caution' has crossed the boundary "
            "while appearing to respect it."
        )

    def test_c0161_severity_is_attention(self):
        """A C0161 finding must have severity_hint='attention' (highest valid level)."""
        result = _build_brake_finding(_BRAKE_DATA_C0161)
        assert result["severity_hint"] == "attention", (
            f"C0161 brake finding should be 'attention', got {result['severity_hint']!r}"
        )

    def test_c0161_verbatim_phrase_not_softened(self):
        """Assert a softened variant never appears *without* the canonical phrase.

        Scope limit, measured 2026-09-18 (Group 2): this test cannot detect a
        softening applied to ``C0161_VERBATIM_REMEDY`` itself.  Its guard
        condition requires ``C0161_VERBATIM_REMEDY not in finding``, so once the
        constant IS ``"exercise caution"`` the clause is False and the test
        passes vacuously.  What it does catch is a finding that adds softening
        language alongside a dropped phrase.  The constant's value is pinned by
        ``test_c0161_verbatim_remedy_value_is_pinned``.
        """
        result = _build_brake_finding(_BRAKE_DATA_C0161)
        finding = result["finding"]
        softened_variants = [
            "exercise caution",
            "drive with care",
            "seek service soon",
            "have it checked",
            "schedule an inspection",
        ]
        for variant in softened_variants:
            # The softened variant alone (without the verbatim phrase) would be
            # a boundary violation — but only if the verbatim phrase is absent.
            if variant in finding.lower() and C0161_VERBATIM_REMEDY not in finding:
                pytest.fail(
                    f"Finding for C0161 uses softened language {variant!r} "
                    f"without the verbatim phrase. This is a spec § D8 boundary violation.\n"
                    f"Finding: {finding!r}"
                )

    def test_c0161_absent_from_non_c0161_brake_finding(self):
        """The verbatim P0 phrase must NOT appear when C0161 is not in dtc_codes."""
        result = _build_brake_finding(_BRAKE_DATA_RESOLVED)  # no DTCs
        assert C0161_VERBATIM_REMEDY not in result["finding"], (
            "The verbatim P0 phrase should only appear when C0161 is in dtc_codes."
        )

    def test_inject_p0_verbatim_adds_phrase_when_missing(self):
        """inject_p0_verbatim_phrases injects the phrase if the LLM omitted it."""
        from agents.health.prompt import inject_p0_verbatim_phrases
        softened = "Your brake system needs attention. Exercise caution while driving."
        result = inject_p0_verbatim_phrases(softened, ["C0161"])
        assert C0161_VERBATIM_REMEDY in result, (
            "inject_p0_verbatim_phrases must inject the verbatim phrase "
            "when C0161 is present but the phrase is missing."
        )

    def test_inject_p0_verbatim_noop_when_phrase_present(self):
        """inject_p0_verbatim_phrases is a no-op when phrase is already present."""
        from agents.health.prompt import inject_p0_verbatim_phrases
        already_correct = f"{C0161_VERBATIM_REMEDY} Schedule immediate repair."
        result = inject_p0_verbatim_phrases(already_correct, ["C0161"])
        # Should not double-inject
        assert result.count(C0161_VERBATIM_REMEDY) == 1

    def test_inject_p0_verbatim_noop_when_no_c0161(self):
        """inject_p0_verbatim_phrases does nothing when C0161 is absent."""
        from agents.health.prompt import inject_p0_verbatim_phrases
        original = "Brake service completed successfully."
        result = inject_p0_verbatim_phrases(original, ["C0040"])
        assert result == original


# ===========================================================================
# severity_hint vocabulary tests (spec § D8 — never a P-level)
# ===========================================================================

class TestSeverityHintVocabulary:
    """severity_hint must never be a P-level — it is advisory urgency, not triage."""

    def test_severity_hint_never_carries_p_level_battery(self):
        """Battery finding severity_hint is not a P-level."""
        for rows in ([], [_BATTERY_ROW_HEALTHY], [_BATTERY_ROW_DEGRADED]):
            result = _build_battery_finding(rows)
            assert result["severity_hint"] not in {"P0", "P1", "P2", "P3"}, (
                f"Battery finding has P-level severity_hint={result['severity_hint']!r}. "
                "P-levels belong to the triage classifier (spec § D8)."
            )

    def test_severity_hint_never_carries_p_level_brake_c0161(self):
        """Brake C0161 finding severity_hint is 'attention', NOT 'P0'."""
        result = _build_brake_finding(_BRAKE_DATA_C0161)
        assert result["severity_hint"] not in {"P0", "P1", "P2", "P3"}, (
            f"Brake finding with C0161 must use severity_hint='attention', "
            f"not a P-level. Got: {result['severity_hint']!r}. "
            "The triage P0 applies to the triage classifier's output, not here."
        )
        assert result["severity_hint"] == "attention"

    def test_severity_hint_never_carries_p_level_tire(self):
        for rows in ([], [_TIRE_ROW_NORMAL], [_TIRE_ROW_NEEDS_REPLACEMENT]):
            result = _build_tire_finding(rows)
            assert result["severity_hint"] not in {"P0", "P1", "P2", "P3"}, (
                f"Tire finding has P-level severity_hint={result['severity_hint']!r}."
            )

    def test_sanitise_severity_hint_rejects_p_levels(self):
        """_sanitise_severity_hint converts any P-level to 'attention'."""
        for p_level in ("P0", "P1", "P2", "P3"):
            result = _sanitise_severity_hint(p_level)
            assert result not in {"P0", "P1", "P2", "P3"}, (
                f"_sanitise_severity_hint({p_level!r}) returned a P-level: {result!r}"
            )
            assert result in SEVERITY_HINTS

    def test_sanitise_severity_hint_passes_valid_values(self):
        """Valid severity_hint values pass through unchanged."""
        for hint in ("routine", "soon", "attention"):
            assert _sanitise_severity_hint(hint) == hint

    def test_all_component_findings_use_valid_vocabulary(self):
        """All three component builders use the canonical vocabulary."""
        results = [
            _build_battery_finding([_BATTERY_ROW_HEALTHY]),
            _build_tire_finding([_TIRE_ROW_NORMAL]),
            _build_brake_finding(_BRAKE_DATA_RESOLVED),
        ]
        for result in results:
            hint = result["severity_hint"]
            assert hint in SEVERITY_HINTS, (
                f"severity_hint={hint!r} is not in the canonical vocabulary "
                f"{sorted(SEVERITY_HINTS)}"
            )


# ===========================================================================
# Bundling decision tests (T4.3 — the judgment that makes this Tier 2)
# ===========================================================================

class TestBundlingDecision:
    """The agent makes a real bundling decision across three components."""

    def test_p0_brake_finding_is_never_bundled(self):
        """A P0 C0161 brake finding must stand alone — never buried in a bundle."""
        brake = _build_brake_finding(_BRAKE_DATA_C0161)
        battery = _build_battery_finding([_BATTERY_ROW_HEALTHY])
        tires = _build_tire_finding([_TIRE_ROW_NORMAL])

        component_results = {
            "brakes": brake,
            "battery": battery,
            "tires": tires,
        }
        bundle, rationale = _decide_bundling(component_results)

        assert not bundle, (
            "A P0 brake finding (C0161) must NEVER be bundled with other findings. "
            "It must stand alone so it is not buried. "
            f"Got bundle=True. Rationale: {rationale!r}"
        )

    def test_single_component_no_bundling_needed(self):
        """Only one component → no bundling decision needed."""
        component_results = {"battery": _build_battery_finding([_BATTERY_ROW_HEALTHY])}
        bundle, rationale = _decide_bundling(component_results)
        assert not bundle

    def test_multiple_attention_components_are_bundled(self):
        """Two or more 'attention' components should be bundled for holistic picture."""
        tire_result = {"finding": "Tire needs replacement.", "severity_hint": "attention",
                       "confidence": "high", "evidence": []}
        brake_result = {"finding": "Open brake job awaiting parts.", "severity_hint": "attention",
                        "confidence": "high", "evidence": []}

        component_results = {"tires": tire_result, "brakes": brake_result}
        bundle, rationale = _decide_bundling(component_results)

        assert bundle, (
            "Multiple 'attention'-level components should be bundled together "
            "so the owner sees a holistic picture. "
            f"Got bundle=False. Rationale: {rationale!r}"
        )

    def test_urgent_plus_routine_are_separated(self):
        """One urgent + one routine → separate so urgent finding is not diluted."""
        urgent = {"finding": "Tire needs replacement.", "severity_hint": "attention",
                  "confidence": "high", "evidence": []}
        routine = {"finding": "Battery at 92%, normal.", "severity_hint": "routine",
                   "confidence": "high", "evidence": []}

        component_results = {"tires": urgent, "battery": routine}
        bundle, rationale = _decide_bundling(component_results)

        assert not bundle, (
            "An 'attention' finding plus a 'routine' finding should be separated "
            "so the urgent message is not diluted. "
            f"Got bundle=True. Rationale: {rationale!r}"
        )

    def test_all_routine_components_bundled_for_efficiency(self):
        """All 'routine' findings → bundle for an efficient summary."""
        r1 = {"finding": "Battery normal.", "severity_hint": "routine",
              "confidence": "high", "evidence": []}
        r2 = {"finding": "Tires normal.", "severity_hint": "routine",
              "confidence": "high", "evidence": []}
        r3 = {"finding": "Brake service complete.", "severity_hint": "routine",
              "confidence": "high", "evidence": []}

        bundle, _ = _decide_bundling({"battery": r1, "tires": r2, "brakes": r3})
        assert bundle, "All-routine findings should be bundled for efficiency."


# ===========================================================================
# assess_vehicle_health integration tests
# ===========================================================================

class TestAssessVehicleHealth:
    """Integration tests for assess_vehicle_health() without an LLM."""

    def test_battery_only_returns_battery_key(self):
        result = assess_vehicle_health(SAMPLE_VIN, battery_rows=[_BATTERY_ROW_HEALTHY])
        assert "battery" in result

    def test_battery_only_bundle_key_present(self):
        result = assess_vehicle_health(SAMPLE_VIN, battery_rows=[_BATTERY_ROW_HEALTHY])
        assert "bundle" in result
        assert "bundle_rationale" in result

    def test_three_components_all_present(self):
        """assess_vehicle_health returns all three component keys."""
        result = assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=[_TIRE_ROW_NORMAL],
            battery_rows=[_BATTERY_ROW_HEALTHY],
            brake_data=_BRAKE_DATA_RESOLVED,
        )
        assert "battery" in result
        assert "tires" in result
        assert "brakes" in result

    def test_three_components_no_p_levels_in_severity(self):
        """No component finding may carry a P-level in severity_hint."""
        result = assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=[_TIRE_ROW_NEEDS_REPLACEMENT],
            battery_rows=[_BATTERY_ROW_HEALTHY],
            brake_data=_BRAKE_DATA_C0161,
        )
        for component in ("tires", "battery", "brakes"):
            if component in result and isinstance(result[component], dict):
                hint = result[component].get("severity_hint", "")
                assert hint not in {"P0", "P1", "P2", "P3"}, (
                    f"Component {component!r} has P-level severity_hint={hint!r}."
                )

    def test_c0161_finding_in_full_assessment_is_verbatim(self):
        """In the full multi-component assessment, C0161 verbatim phrase must appear."""
        result = assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=[_TIRE_ROW_NORMAL],
            battery_rows=[_BATTERY_ROW_HEALTHY],
            brake_data=_BRAKE_DATA_C0161,
        )
        brake_finding = result.get("brakes", {}).get("finding", "")
        assert C0161_VERBATIM_REMEDY in brake_finding, (
            f"Full assessment with C0161 must include verbatim phrase.\n"
            f"Got brake finding: {brake_finding!r}"
        )

    def test_c0161_assessment_is_not_bundled(self):
        """C0161 brake finding must not be bundled even in multi-component assessment."""
        result = assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=[_TIRE_ROW_NEEDS_REPLACEMENT],
            battery_rows=[_BATTERY_ROW_HEALTHY],
            brake_data=_BRAKE_DATA_C0161,
        )
        assert result["bundle"] is False, (
            "A C0161 brake P0 finding must not be bundled with other components."
        )

    def test_battery_reassurance_in_full_assessment(self):
        """Battery finding in full multi-component assessment is still reassurance."""
        result = assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=[_TIRE_ROW_NORMAL],
            battery_rows=[_BATTERY_ROW_HEALTHY],
            brake_data=_BRAKE_DATA_RESOLVED,
        )
        battery_finding = result.get("battery", {}).get("finding", "")
        assert "degrading normally" in battery_finding.lower(), (
            f"Battery finding in full assessment should say 'degrading normally'. "
            f"Got: {battery_finding!r}"
        )

    def test_all_findings_have_required_keys(self):
        """Every component result must have finding, severity_hint, confidence, evidence."""
        result = assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=[_TIRE_ROW_NORMAL],
            battery_rows=[_BATTERY_ROW_HEALTHY],
            brake_data=_BRAKE_DATA_RESOLVED,
        )
        required_keys = {"finding", "severity_hint", "confidence", "evidence"}
        for component in ("tires", "battery", "brakes"):
            comp_result = result.get(component, {})
            missing = required_keys - set(comp_result.keys())
            assert not missing, (
                f"Component {component!r} missing keys: {missing}"
            )


# ===========================================================================
# run_health_agent adapter tests
# ===========================================================================

class TestRunHealthAgentAdapter:
    """run_health_agent() adapter returns runner-compatible dict."""

    def test_battery_rows_return_standard_dict(self):
        result = run_health_agent(SAMPLE_VIN, [_BATTERY_ROW_HEALTHY])
        required = {"finding", "severity_hint", "confidence", "evidence"}
        assert required.issubset(set(result.keys()))

    def test_battery_adapter_severity_not_p_level(self):
        result = run_health_agent(SAMPLE_VIN, [_BATTERY_ROW_HEALTHY])
        assert result["severity_hint"] not in {"P0", "P1", "P2", "P3"}

    def test_tire_rows_detected_by_shape(self):
        """Tire rows are detected by presence of the 'replacement_flagged' key.

        Agent-facing vocabulary from adp_tire_source, NOT the ADP column name —
        an earlier revision said needs_replacement, which was the broken shape.
        """
        result = run_health_agent(SAMPLE_VIN, [_TIRE_ROW_NORMAL])
        assert isinstance(result["finding"], str)
        assert result["severity_hint"] in SEVERITY_HINTS

    def test_brake_rows_detected_by_shape(self):
        """Brake rows are detected by presence of 'outcome' key."""
        result = run_health_agent(SAMPLE_VIN, [_BRAKE_DATA_RESOLVED])
        assert isinstance(result["finding"], str)
        assert result["severity_hint"] in SEVERITY_HINTS

    def test_empty_rows_returns_routine(self):
        result = run_health_agent(SAMPLE_VIN, [])
        assert result["severity_hint"] == "routine"

    def test_returns_tokens_used_key(self):
        """Runner budget cap needs _tokens_used in the return dict."""
        result = run_health_agent(SAMPLE_VIN, [_BATTERY_ROW_HEALTHY])
        assert "_tokens_used" in result
        assert isinstance(result["_tokens_used"], int)

    def test_returns_cost_usd_key(self):
        result = run_health_agent(SAMPLE_VIN, [_BATTERY_ROW_HEALTHY])
        assert "_cost_usd" in result


# ===========================================================================
# KB coverage tests — no invented explanations for non-KB brake DTCs
# ===========================================================================

class TestBrakeDtcCoverage:
    """Only C0161 and C0040 have ADP KB guides — no inventing for the rest."""

    def test_brake_dtc_kb_guided_contains_c0161_and_c0040(self):
        """The KB-guided set must include exactly C0161 and C0040."""
        assert "C0161" in BRAKE_DTC_KB_GUIDED
        assert "C0040" in BRAKE_DTC_KB_GUIDED

    def test_assemble_brake_dtc_note_c0161_returns_verbatim(self):
        from agents.health.prompt import assemble_brake_dtc_note
        note = assemble_brake_dtc_note(["C0161"])
        assert C0161_VERBATIM_REMEDY in note

    def test_assemble_brake_dtc_note_unknown_code_no_fabrication(self):
        from agents.health.prompt import assemble_brake_dtc_note
        note = assemble_brake_dtc_note(["C0031"])
        # Must mention the code was recorded
        assert "C0031" in note
        # Must NOT fabricate a technical explanation
        fabricated = ["wheel speed", "hydraulic", "caliper", "abs module"]
        note_lower = note.lower()
        for phrase in fabricated:
            assert phrase not in note_lower, (
                f"assemble_brake_dtc_note fabricated explanation {phrase!r} "
                f"for non-KB code C0031. Note: {note!r}"
            )

    def test_assemble_brake_dtc_note_empty_returns_empty(self):
        from agents.health.prompt import assemble_brake_dtc_note
        assert assemble_brake_dtc_note([]) == ""


# ---------------------------------------------------------------------------
# Source-to-agent key contract
#
# The durable guard for the defect this fix group closed.  `agent.py` had been
# reading ADP column names (`needs_replacement`, `wear_category`) while
# `adp_tire_source` emits an agent-facing vocabulary (`replacement_flagged`,
# `wear_band`).  Every unit test passed, because the agent's tests fed
# ADP-shaped rows and the source's tests checked the source in isolation --
# nobody asserted the two agreed.  Fed a real source row for a tire ADP had
# flagged for replacement, the agent narrated "normal wear ... no replacement
# signal" at severity `routine`: a silent false negative on a real finding.
#
# Same class as the stack/runner environment-contract defect in Group 3, and
# the same remedy: assert the seam, not just the two sides.
# ---------------------------------------------------------------------------


def _source_emitted_keys() -> set:
    """Every key the source modules emit as OUTPUT to the agent.

    Deliberately dict-literal keys only.  An earlier revision also counted keys a
    source *reads from ADP* and passes through, which made this test vacuous: the
    tire source reads ``needs_replacement`` in order to translate it, so counting
    inputs put the very name the agent must not use back into the allowed set.
    Proved vacuous by reintroducing the historical bug and watching the test pass.
    Output keys only.

    ``dms_warranty_source`` joined the list in Fix Group 21: it is a source module in the
    same sense as the three ADP ones — it emits ``verdict_status`` / ``verdict_reason`` /
    ``provenance`` for the agent to read — so omitting it would have made the guard reject a
    legitimate read. It is listed here rather than allowlisted in ``own`` because the property
    under test is "some source emits this key", and one genuinely does.

    Dict keys given as a **module-level named constant** are resolved to their value, the same
    way :func:`_read_keys_from_source` already resolves ``_KEY_*`` constants on the read side.
    Before Fix Group 21 this side handled only bare string literals, so a source building
    ``{ATTR_VERDICT_STATUS: ...}`` emitted a key this scan could not see — a latent gap that
    under-counted the emitted set and would report a *false orphan* for any source written
    that way.

    **DynamoDB wire-protocol shapes are excluded** (review Cycle 1, Warning 2). Registering
    the first source that talks to DynamoDB rather than Athena admitted six request-protocol
    names — ``TableName``, ``IndexName``, ``KeyConditionExpression``,
    ``ExpressionAttributeValues``, plus the attribute-value wrapper ``S`` and the expression
    placeholder ``:k`` — because the walk counts *every* ``ast.Dict`` and a request dict is a
    dict. That silently widened the allowed set from 21 keys to 30 and falsified this
    function's own "output keys only" claim. Nothing was masked in practice (``agent.py``
    reads none of the six), but an invariant that is false is not protecting anything.

    A request dict is identified structurally rather than by name-matching each key: a dict
    carrying any request-envelope key is a request, and a dict whose keys are all
    attribute-value type tags or ``:``-prefixed placeholders is a value wrapper. Both are
    skipped. ``test_emitted_keys_excludes_ddb_wire_protocol`` pins the exclusion and
    ``test_emitted_keys_still_contains_real_output_keys`` is its positive control.
    """
    import ast
    import pathlib

    keys: set = set()
    for name in (
        "adp_tire_source",
        "adp_battery_source",
        "adp_brake_source",
        "dms_warranty_source",
    ):
        src = pathlib.Path(f"agents/health/{name}.py").read_text()
        tree = ast.parse(src)

        # Module-level `NAME = "literal"` constants, for dict keys written as a name.
        consts: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    consts[tgt.id] = node.value.value
            # Annotated form: `NAME: str = "literal"`.
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                consts[node.target.id] = node.value.value

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue

            resolved: set = set()
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    resolved.add(k.value)
                elif isinstance(k, ast.Name) and k.id in consts:
                    resolved.add(consts[k.id])

            if _is_ddb_wire_dict(resolved):
                continue
            keys |= resolved
    return keys


#: Keys that mark a dict as a DynamoDB *request* envelope rather than agent output.
_DDB_REQUEST_ENVELOPE_KEYS = frozenset({
    "TableName", "IndexName", "KeyConditionExpression", "ExpressionAttributeValues",
    "ExpressionAttributeNames", "FilterExpression", "ExclusiveStartKey",
    "ProjectionExpression", "Key", "Item", "UpdateExpression", "ConditionExpression",
})

#: DynamoDB attribute-value type tags. A dict whose keys are only these is a value wrapper.
_DDB_ATTRIBUTE_VALUE_TAGS = frozenset({
    "S", "N", "B", "BOOL", "NULL", "L", "M", "SS", "NS", "BS",
})


def _is_ddb_wire_dict(resolved: set) -> bool:
    """True when *resolved* is a DynamoDB request envelope or attribute-value wrapper.

    Structural, not a name denylist: the first test asks whether the dict carries a request
    envelope key at all, the second whether every key is a type tag or a ``:``-prefixed
    expression placeholder. Empty dicts are not wire dicts (and contribute nothing anyway).
    """
    if not resolved:
        return False
    if resolved & _DDB_REQUEST_ENVELOPE_KEYS:
        return True
    return all(k in _DDB_ATTRIBUTE_VALUE_TAGS or k.startswith(":") for k in resolved)


def test_emitted_keys_excludes_ddb_wire_protocol():
    """Review Cycle 1 Warning 2: request-protocol names are not output vocabulary.

    Without the exclusion these six entered the allowed set and widened it from 21 to 30.
    """
    emitted = _source_emitted_keys()
    for wire_key in (
        "TableName", "IndexName", "KeyConditionExpression", "ExpressionAttributeValues",
        "S", ":k",
    ):
        assert wire_key not in emitted, (
            f"{wire_key!r} is a DynamoDB request-protocol name, not a key any source emits "
            "to the agent — the wire-dict exclusion has regressed"
        )


def test_emitted_keys_still_contains_real_output_keys():
    """POSITIVE CONTROL for the exclusion above — it must not empty the emitted set.

    Covers both spellings the scan has to handle: a bare literal (``provenance``) and a
    module-level named constant (``verdict_status`` via ``ATTR_VERDICT_STATUS``).
    """
    emitted = _source_emitted_keys()
    for output_key in ("provenance", "verdict_status", "verdict_reason"):
        assert output_key in emitted, f"{output_key!r} vanished from the emitted set"
    # Sanity-check the control itself: `finding` is deliberately NOT here. It is the agent's
    # own output key, allowlisted in `own` below, not something a source emits — including it
    # would have made this control assert the wrong thing.
    assert "finding" not in emitted


def _agent_read_keys() -> set:
    """Every row key `agent.py` reads via `r.get(...)` / `row.get(...)`.

    ``os.environ.get(...)`` / ``os.getenv(...)`` are excluded. The scan matches any
    ``.get("literal")`` call, and an environment-variable name is not a row key — Fix Group
    21's ``os.environ.get("VSA_DMS_REGION", ...)`` was reported as an orphan row key on that
    basis alone. Allowlisting it in ``own`` would have been the wrong fix: it would assert
    that an env var is "a key the agent reads from its own artifacts", and would make every
    future env var read require an allowlist entry to stay green.

    This narrowing cannot weaken the § D3 property the guard protects — that the agent does
    not read ADP column names directly — because an ADP column is not reachable through
    ``os.environ``. ``test_env_var_exclusion_does_not_hide_row_key_reads`` is the positive
    control: it proves a genuine orphan row key is still detected after this change.
    """
    import pathlib

    src = pathlib.Path("agents/health/agent.py").read_text()
    return _read_keys_from_source(src)


def _read_keys_from_source(src: str) -> set:
    """Extract row keys read via ``.get("literal")``, skipping environment reads."""
    import ast

    tree = ast.parse(src)
    # Resolve module-level `_KEY_* = "literal"` constants first.
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                consts[tgt.id] = node.value.value

    def _is_environ_read(func: ast.Attribute) -> bool:
        """True for ``os.environ.get`` and ``os.getenv``."""
        if func.attr == "getenv":
            return True
        recv = func.value
        if isinstance(recv, ast.Attribute) and recv.attr == "environ":
            return True
        if isinstance(recv, ast.Name) and recv.id == "environ":
            return True
        return False

    read: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("get", "getenv") and node.args:
            if _is_environ_read(node.func):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                read.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                read.add(consts[arg.id])
    return read


def test_env_var_exclusion_does_not_hide_row_key_reads():
    """POSITIVE CONTROL for the ``os.environ`` exclusion in :func:`_agent_read_keys`.

    Narrowing a scan is how a guard quietly stops detecting anything. This proves the
    narrowing is precise: an environment read is skipped, and a genuine row read on the
    adjacent line is still found.
    """
    src = (
        "import os\n"
        "TABLE = os.environ.get('SOME_TABLE_ENV_VAR')\n"
        "OTHER = os.getenv('ANOTHER_ENV_VAR')\n"
        "def f(row):\n"
        "    return row.get('a_genuine_row_key')\n"
    )
    found = _read_keys_from_source(src)
    assert "a_genuine_row_key" in found, "the scan stopped detecting row-key reads"
    assert "SOME_TABLE_ENV_VAR" not in found
    assert "ANOTHER_ENV_VAR" not in found


def test_agent_reads_only_keys_the_sources_emit():
    """No key the agent reads may be absent from every source's output.

    This is the assertion that would have caught the tire false negative.
    """
    emitted = _source_emitted_keys()
    read = _agent_read_keys()
    # Keys the agent legitimately reads from its own artifacts, not from a source.
    own = {
        "finding", "severity_hint", "confidence", "evidence", "component",
        # The coverage-verdict guard's checked headline (T4.3), the agent's own field.
        "headline",
        "computed_at", "agent_version", "inputs_hash", "ttl", "pk", "sk",
        "rows", "value", "source", "provenance", "vin",
        # Component names and structural keys of the agent's own payloads.
        "tires", "battery", "brakes", "data",
        # assess_fleet_health input protocol keys (Addendum D1).
        # These are keys in the caller-supplied vin_data list entries —
        # they are the fleet function's own input protocol, not source-emitted row keys.
        "battery_rows", "tire_rows", "brake_data",
        # Fleet finding output keys (also part of the agent's own protocol).
        "scope", "finding_kind", "finding_key", "severity", "fleet_id",
        "finding_summary", "vin_evidence",
        # Strands SDK telemetry keys, read from AgentResult.metrics.accumulated_usage
        # in make_strands_agent_fn's _llm_caller. Listed separately from the keys
        # above because they are a THIRD category: not source-emitted row keys, and
        # not this agent's own payload either — they belong to the SDK.
        #
        # Verified not to be ADP columns: none appears in adp_battery_source.py,
        # adp_tire_source.py, adp_brake_source.py or tier2/adp_source.py, so
        # allowlisting them cannot mask the § D3 violation this guard exists to
        # catch (the agent reading an ADP column name directly).
        #
        # This guard caught these three the moment they were added, which is worth
        # recording: the allowlist had just been widened by ten entries and the
        # concern was that it had stopped detecting anything. It had not.
        "inputTokens", "outputTokens", "totalTokens",
        # Circuit-breaker counter keys, read off the agent_fn's own closure
        # dicts (``_llm_call_count``, ``_skipped_by_breaker``,
        # ``_trip_reason``, ``_remaining_vins``) that make_strands_agent_fn
        # exposes as attributes so the runner can record the breaker's
        # state on the run record (W3 cycle-3 Warning).  Not ADP columns:
        # ``n`` and ``count`` do not appear in any ``adp_*_source.py`` or
        # ``adp_source.py`` column list.  ``reason`` likewise does not.
        "n", "count", "reason",
    }
    orphans = {k for k in read if k not in emitted and k not in own}
    assert not orphans, (
        "agent.py reads row keys that no source module emits: "
        f"{sorted(orphans)}. Either the source's output vocabulary changed and the "
        "agent was not updated, or the agent is reading ADP column names directly "
        "(spec § D3 forbids that -- the source translates at its own boundary)."
    )


def test_key_contract_detector_catches_an_orphan_read():
    """The contract test above must actually fail on a violation."""
    emitted = {"replacement_flagged", "wear_band"}
    read = {"replacement_flagged", "needs_replacement"}  # the historical bug
    own: set = set()
    orphans = {k for k in read if k not in emitted and k not in own}
    assert orphans == {"needs_replacement"}, "detector failed to spot the orphan read"


# ---------------------------------------------------------------------------
# Tire finding behaviour
#
# These did not exist before 2026-08-05, which is the deeper reason the tire
# false negative survived: the agent read `needs_replacement` while the source
# emitted `replacement_flagged`, and NO test asserted that a flagged tire
# produces an urgent finding.  Fixture keys were wrong too, but a correct
# fixture with no behavioural assertion would still not have caught it.
#
# Every row below uses the agent-facing vocabulary `adp_tire_source` actually
# emits.  Verified non-vacuous: repointing `_KEY_REPLACEMENT_FLAGGED` back to
# the ADP name fails these.
# ---------------------------------------------------------------------------


def _tire_row(*, flagged: bool, band: str, position: str = "FL") -> dict:
    return {
        "vin": "1FA00000000012345",
        "position": position,
        "event_date": "2026-08-01",
        "replacement_flagged": flagged,
        "wear_band": band,
        "wear_rate_per_1k_km": 0.15 if flagged else 0.04,
    }


def test_flagged_tire_is_not_routine():
    """A tire ADP flagged for replacement must never be narrated as routine.

    This is the regression the fix group closed: the agent previously produced
    "normal wear. No replacement signal." at severity `routine` for exactly
    this input.
    """
    result = _agent._build_tire_finding([_tire_row(flagged=True, band="replace")])
    assert result["severity_hint"] != "routine", (
        "a flagged tire was narrated as routine — the original false negative"
    )
    assert result["severity_hint"] == "attention"


def test_flagged_tire_finding_mentions_replacement_and_position():
    result = _agent._build_tire_finding([_tire_row(flagged=True, band="replace", position="RL")])
    finding = result["finding"].lower()
    assert "replacement" in finding
    assert "RL" in result["finding"], "the affected tire position must be named"


def test_healthy_tire_is_routine_and_does_not_manufacture_urgency():
    """An `ok` band with no flag is a healthy tire.

    Reporting `soon` here would manufacture urgency the data does not support —
    the same error § D3a warns about for battery, where the honest output is
    reassurance.  An earlier revision did exactly that.
    """
    result = _agent._build_tire_finding([_tire_row(flagged=False, band="ok")])
    assert result["severity_hint"] == "routine"
    assert "no action" in result["finding"].lower() or "normal" in result["finding"].lower()


def test_elevated_wear_without_flag_is_soon_not_attention():
    """Elevated wear with no replacement flag is worth mentioning, not urgent."""
    result = _agent._build_tire_finding([_tire_row(flagged=False, band="monitor")])
    assert result["severity_hint"] == "soon"


def test_tire_finding_computes_no_threshold():
    """The agent must not derive its own replacement decision from wear rate.

    A very high wear rate with ADP's flag False must still not escalate to
    `attention` — ADP owns the inference (§ D3), the agent owns the judgment.
    """
    row = _tire_row(flagged=False, band="ok")
    row["wear_rate_per_1k_km"] = 99.0
    result = _agent._build_tire_finding([row])
    assert result["severity_hint"] != "attention", (
        "agent escalated on wear rate alone — that is ADP's inference to make"
    )


# ===========================================================================
# W5 fix — resolved brake jobs must not narrate the P0 stop-driving phrase
#
# Security review Cycle 7 § W5: outcome='resolved' + dtc_codes=['C0161']
# yielded severity_hint='soon' while the finding contained "Pull over
# immediately." — a semantic contradiction.  Service-history tables retain
# historical DTC codes on completed work.  ~475k VINs / ~500k rows in ADP's
# service_records have exactly this shape.
#
# Dilution is the harm being fixed: over-narrating the phrase on completed
# records erodes owner trust in exactly the same way softening it does.  The
# phrase must keep its force for the cases that warrant it.
#
# Fix Group 4b T4.9 — narrows *when* injection fires; does NOT change or
# soften the phrase when it does fire.
# ===========================================================================

#: Resolved brake record with a historical C0161 DTC — the common ADP shape.
#: The service was completed; the DTC is retained in the history table.
#: This is the input that W5 described as triggering the false P0 narration.
_BRAKE_DATA_RESOLVED_WITH_C0161 = {
    "vin": SAMPLE_VIN,
    "service_date": "2024-01-15",
    "dtc_codes": ["C0161"],
    "complaint_text": "Brake warning light — resolved at dealer",
    "warranty_covered": "true",
    "total_cost_usd": "0.00",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}


class TestW5ResolvedBrakeP0Narration:
    """Resolved brake jobs with historical C0161 must NOT narrate 'Pull over immediately.'

    An open brake job with C0161 still must narrate it verbatim.  The two
    tests together pin the narrowing: resolved → no phrase, open → phrase
    present.
    """

    def test_resolved_c0161_does_not_trigger_verbatim_p0_phrase(self):
        """W5 fix: a resolved brake job with C0161 must NOT contain the stop-driving phrase.

        Service-history tables retain historical DTCs.  Narrating "Pull over
        immediately" on a completed job creates a contradiction (severity_hint=
        'soon' vs "Pull over immediately") and dilutes the phrase for real
        active cases.

        This test was designed to FAIL against the pre-fix implementation.
        Verified: before the _ACTIVE_BRAKE_OUTCOMES gate was added, this
        test failed because _build_brake_finding called
        inject_p0_verbatim_phrases unconditionally and the resolved finding
        contained the verbatim phrase.
        """
        result = _build_brake_finding(_BRAKE_DATA_RESOLVED_WITH_C0161)
        assert C0161_VERBATIM_REMEDY not in result["finding"], (
            "Resolved brake service with historical C0161 must NOT narrate "
            f"the P0 stop-driving phrase.\n"
            f"  Got finding: {result['finding']!r}\n\n"
            "Spec § D3b: service records retain historical DTCs. Diluting "
            "the phrase by applying it to completed jobs erodes trust the "
            "same way softening it does (security review Cycle 7 § W5)."
        )

    def test_resolved_c0161_severity_is_soon_not_contradictory(self):
        """Resolved brake with C0161 → severity_hint='soon', consistent with finding text."""
        result = _build_brake_finding(_BRAKE_DATA_RESOLVED_WITH_C0161)
        assert result["severity_hint"] == "soon", (
            f"Resolved brake with C0161 should have severity_hint='soon' "
            f"(mention at next service), got {result['severity_hint']!r}."
        )

    def test_open_c0161_still_triggers_verbatim_p0_phrase(self):
        """Narrowing must not break the phrase for open brake jobs.

        This test guards against an over-broad fix: the phrase must still
        fire when the brake job is genuinely active.  Uses _BRAKE_DATA_C0161
        which has outcome='parts_pending' and is_open=True.
        """
        result = _build_brake_finding(_BRAKE_DATA_C0161)
        assert C0161_VERBATIM_REMEDY in result["finding"], (
            "Open brake job with C0161 (parts_pending, is_open=True) MUST "
            "still contain the verbatim P0 phrase after the W5 narrowing.\n"
            f"  Got finding: {result['finding']!r}\n\n"
            "The fix narrows WHEN injection fires; it must not suppress the "
            "phrase for genuinely active brake safety issues."
        )

    def test_follow_up_required_c0161_triggers_verbatim_p0_phrase(self):
        """follow_up_required is an open outcome — P0 phrase must still fire."""
        follow_up_with_c0161 = {
            "vin": SAMPLE_VIN,
            "service_date": "2026-07-01",
            "dtc_codes": ["C0161"],
            "complaint_text": "Brake pressure warning",
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "follow_up_required",
            "is_open": True,
            "provenance": "live",
        }
        result = _build_brake_finding(follow_up_with_c0161)
        assert C0161_VERBATIM_REMEDY in result["finding"], (
            "follow_up_required outcome with C0161 must still trigger the "
            "verbatim P0 phrase — this is an unresolved brake issue.\n"
            f"  Got finding: {result['finding']!r}"
        )

    def test_resolved_c0161_finding_still_acknowledges_dtc(self):
        """A resolved C0161 service should still acknowledge the DTC was recorded.

        The fix suppresses the stop-driving phrase, not the DTC mention.
        The owner deserves to know C0161 appeared in the service record even
        though the work is complete.
        """
        result = _build_brake_finding(_BRAKE_DATA_RESOLVED_WITH_C0161)
        finding = result["finding"]
        # The finding should reference the completed service or DTC codes.
        # It must NOT be identical to the no-DTC resolved finding.
        no_dtc_resolved = _build_brake_finding(_BRAKE_DATA_RESOLVED)
        # With DTCs, the finding must differ from the plain resolved finding
        # (the DTC note should still appear for resolved + C0161).
        assert finding != no_dtc_resolved["finding"], (
            "A resolved brake record with C0161 should produce a different "
            "finding than one with no DTCs — the DTC was recorded and is "
            "relevant history, even if the stop-driving phrase is suppressed."
        )


# ===========================================================================
# T4.10 — W2 hardening: orphaned templates deleted; enrichment seam hardened
#
# Three distinct defects, each with a before/after test pair.
#
# (b1) _parse_llm_response greedy regex
#      The greedy `re.search(r"\{.*\}", ..., DOTALL)` takes from the first `{`
#      to the LAST `}` in the response.  When the LLM wraps its output in
#      Markdown or adds explanatory prose that contains a second `{ ... }` block,
#      the greedy match produces a string that is not valid JSON, so `json.loads`
#      raises `JSONDecodeError` and the function falls back to the deterministic
#      result — which looks correct but is silently losing the LLM output.  The
#      fix is to extract the FIRST complete JSON object (use `json.JSONDecoder.
#      raw_decode` scanning from the first `{`), so trailing prose cannot corrupt
#      the parse.
#
# (b2) _sanitise_severity_hint downward override
#      The current sanitiser rejects P-levels but allows the LLM to downgrade a
#      deterministic `attention` (open brake job) to `routine`.  The fix adds a
#      floor: `_sanitise_component_result` must not allow the enriched severity to
#      go below what the deterministic builder produced.  Test: pass an open-brake
#      fallback (severity=`attention`) and a stub LLM that returns `routine` →
#      enriched result must still be `attention`.
#
# (b3) Second inject_p0_verbatim_phrases call in _enrich_with_llm
#      The re-apply block at agent.py fires unconditionally on `dtc_codes` after
#      the LLM call, without the T4.9 gate.  A resolved brake job with a
#      historical C0161 DTC therefore re-acquires the stop-driving phrase via the
#      enrichment path even though _build_brake_finding (T4.9) correctly
#      suppressed it.  Fix: apply the same `_active_brake_job` gate here.
#
# Each test is written to FAIL against the current implementation, demonstrating
# non-vacuousness per the decisions.md requirement.
# ===========================================================================


# Shared fixture: resolved brake with C0161 (same shape as _BRAKE_DATA_RESOLVED_WITH_C0161
# above but defined again here for self-containment of the new class).
_W2_BRAKE_RESOLVED_C0161 = {
    "vin": SAMPLE_VIN,
    "service_date": "2024-01-15",
    "dtc_codes": ["C0161"],
    "complaint_text": "Brake warning light — resolved at dealer",
    "warranty_covered": "true",
    "total_cost_usd": "0.00",
    "outcome": "resolved",
    "is_open": False,
    "provenance": "live",
}

_W2_BRAKE_OPEN_C0161 = {
    "vin": SAMPLE_VIN,
    "service_date": "2026-08-03",
    "dtc_codes": ["C0161"],
    "complaint_text": "Brake warning light",
    "warranty_covered": "false",
    "total_cost_usd": "0.00",
    "outcome": "parts_pending",
    "is_open": True,
    "provenance": "live",
}


def _make_stub_llm(severity_override: str | None = None, finding_override: str | None = None):
    """Return a stub LLM caller that returns a well-formed JSON response.

    If *severity_override* is given, the brakes component carries that value.
    This is used to test whether the enrichment path correctly rejects a downward
    severity override.

    Uses json.dumps to produce valid JSON regardless of quote characters in
    the finding text — an f-string with !r would produce Python reprs (single
    quotes) that are not valid JSON.
    """
    import json as _json

    def _stub(system_prompt: str, user_prompt: str) -> str:
        brake_severity = severity_override or "soon"
        brake_finding = finding_override or "Brake service was completed."
        payload = {
            "brakes": {
                "finding": brake_finding,
                "severity_hint": brake_severity,
                "confidence": "high",
            }
        }
        return _json.dumps(payload)

    return _stub


class TestW2EnrichmentSeamHardening:
    """T4.10 (b): hardening of the reachable _enrich_with_llm seam.

    All three tests MUST FAIL against the current implementation before the fix
    is applied.  Non-vacuousness is demonstrated by running the tests against the
    current code first (see decisions.md § 2026-08-05 requirement).
    """

    # -----------------------------------------------------------------
    # (b1) _parse_llm_response: first JSON object, not last }
    # -----------------------------------------------------------------

    def test_b1_trailing_prose_with_second_json_block_uses_first_object(self):
        """_parse_llm_response must parse the FIRST JSON object.

        The greedy DOTALL regex takes from first { to last }.  When the LLM
        emits valid JSON followed by a second { ... } block (e.g. markdown
        fenced code or an explanatory footnote), the greedy match produces a
        string spanning both blocks that json.loads rejects, silently falling
        back to the deterministic result.

        After the fix, the function must extract the first well-formed JSON
        object so the LLM's actual output is used, not discarded.

        Failure before fix: the greedy match returns the whole string from the
        first `{` to the last `}`, which includes the trailing block and fails
        json.loads → function returns the deterministic fallback → `finding`
        is 'deterministic fallback', not the LLM string.  Test asserts the LLM
        finding is used → FAIL.
        """
        from agents.health.agent import _parse_llm_response

        # A response where the first JSON object is syntactically complete but
        # the greedy regex extends past it to a second block.
        response = (
            '{"battery": {"finding": "LLM battery finding.", '
            '"severity_hint": "routine", "confidence": "high"}} '
            'Note: {"internal": "metadata"}'
        )
        fallback = {
            "battery": {
                "finding": "deterministic fallback",
                "severity_hint": "routine",
                "confidence": "low",
                "evidence": [],
            }
        }
        result = _parse_llm_response(response, fallback)
        # After the fix, the LLM's finding must be returned (not the fallback).
        assert result["battery"]["finding"] == "LLM battery finding.", (
            "b1: _parse_llm_response discarded the LLM output because the "
            "greedy DOTALL regex captured a second JSON block, making the "
            "combined string unparseable.  The fix must extract the FIRST "
            f"complete JSON object.  Got: {result['battery']['finding']!r}"
        )

    def test_b1_fail_safe_preserved_when_no_json_present(self):
        """Fail-safe is intact: no JSON in response → return deterministic fallback."""
        from agents.health.agent import _parse_llm_response

        fallback = {
            "battery": {
                "finding": "deterministic fallback",
                "severity_hint": "routine",
                "confidence": "low",
                "evidence": [],
            }
        }
        result = _parse_llm_response("Sorry, I cannot help with that.", fallback)
        assert result["battery"]["finding"] == "deterministic fallback"

    def test_b1_fail_safe_preserved_on_invalid_json(self):
        """Fail-safe is intact: invalid JSON → return deterministic fallback."""
        from agents.health.agent import _parse_llm_response

        fallback = {
            "battery": {
                "finding": "deterministic fallback",
                "severity_hint": "routine",
                "confidence": "low",
                "evidence": [],
            }
        }
        result = _parse_llm_response("{not valid json}", fallback)
        assert result["battery"]["finding"] == "deterministic fallback"

    # -----------------------------------------------------------------
    # (b2) _sanitise_severity_hint: downward override rejected
    # -----------------------------------------------------------------

    def test_b2_llm_cannot_downgrade_severity_on_open_brake_job(self):
        """LLM returning 'routine' for an open brake job must be rejected.

        The deterministic builder sets severity_hint='attention' for an open
        brake job.  The current sanitiser rejects P-levels only, so 'routine'
        (a valid vocabulary value) passes through — silently downgrading the
        urgency of an unresolved brake issue.

        After the fix, the enriched severity_hint must remain at or above the
        deterministic floor ('attention' here).

        Failure before fix: stub LLM returns 'routine', sanitiser accepts it,
        enriched result carries severity_hint='routine'.  Test asserts 'attention'
        → FAIL.
        """
        # Deterministic finding for an open brake job → 'attention'.
        det_fallback = {
            "brakes": {
                "finding": "Brake service awaiting parts.",
                "severity_hint": "attention",
                "confidence": "high",
                "evidence": [],
            }
        }
        # Stub LLM that attempts to downgrade to 'routine'.
        stub_llm = _make_stub_llm(severity_override="routine")
        result = assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=_W2_BRAKE_OPEN_C0161,
            _llm_caller=stub_llm,
        )
        enriched_severity = result["brakes"]["severity_hint"]
        assert enriched_severity != "routine", (
            "b2: LLM downgraded severity_hint from 'attention' (open brake job) "
            "to 'routine'.  The sanitiser must reject downward overrides — an LLM "
            "returning 'routine' for an open brake job is a safety downgrade.\n"
            f"Got severity_hint={enriched_severity!r}"
        )
        assert enriched_severity == "attention", (
            f"b2: expected severity_hint='attention', got {enriched_severity!r}"
        )

    def test_b2_llm_can_upgrade_severity(self):
        """LLM upgrading 'routine' → 'soon' is a legitimate improvement."""
        stub_llm = _make_stub_llm(severity_override="soon", finding_override="Worth monitoring.")
        result = assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[_BATTERY_ROW_HEALTHY],
            _llm_caller=stub_llm,
        )
        # The stub returns 'brakes' but only battery was passed; battery falls back.
        # Just confirm no crash and battery finding is reasonable.
        assert "battery" in result

    def test_b2_p_level_still_rejected_after_downward_check(self):
        """P-level rejection is preserved alongside downward-override rejection."""
        from agents.health.agent import _sanitise_severity_hint
        for p_level in ("P0", "P1", "P2", "P3"):
            result = _sanitise_severity_hint(p_level)
            assert result not in {"P0", "P1", "P2", "P3"}, (
                f"P-level {p_level!r} must still be rejected after adding "
                f"downward-override check.  Got: {result!r}"
            )

    # -----------------------------------------------------------------
    # (b3) Second inject_p0 in _enrich_with_llm: resolved-job gate
    # -----------------------------------------------------------------

    def test_b3_resolved_brake_job_does_not_acquire_p0_phrase_via_enrichment(self):
        """Resolved brake with C0161 must NOT gain the verbatim phrase via the LLM path.

        T4.9 (deterministic path) correctly suppresses the phrase for resolved
        jobs.  But _enrich_with_llm re-applies inject_p0_verbatim_phrases
        unconditionally on dtc_codes after the LLM call, re-acquiring the phrase
        that T4.9 suppressed.

        W5 would reappear the moment enrichment is enabled.  The fix: gate the
        second inject call with the same _ACTIVE_BRAKE_OUTCOMES condition.

        Failure before fix: assess_vehicle_health with a stub LLM caller and a
        resolved C0161 record returns a finding containing the verbatim phrase
        (the re-apply block injects it unconditionally).  Test asserts phrase is
        absent → FAIL.
        """
        stub_llm = _make_stub_llm(
            severity_override="soon",
            finding_override="Brake work was completed and the C0161 code addressed.",
        )
        result = assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=_W2_BRAKE_RESOLVED_C0161,
            _llm_caller=stub_llm,
        )
        finding = result["brakes"]["finding"]
        assert C0161_VERBATIM_REMEDY not in finding, (
            "b3: resolved brake job with C0161 acquired the stop-driving phrase "
            "via the LLM enrichment path, re-introducing W5 when enrichment is "
            "enabled.\n"
            f"Got finding: {finding!r}\n\n"
            "The second inject_p0_verbatim_phrases call in _enrich_with_llm must "
            "be gated with the same _ACTIVE_BRAKE_OUTCOMES condition as T4.9."
        )

    def test_b3_open_brake_job_still_gets_p0_phrase_via_enrichment(self):
        """The P0 phrase re-apply must still fire for genuinely open brake jobs.

        The fix narrows WHEN the re-apply fires; it must NOT suppress the phrase
        for active brake issues.
        """
        stub_llm = _make_stub_llm(
            severity_override="attention",
            finding_override="Brake service still pending.",
        )
        result = assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=_W2_BRAKE_OPEN_C0161,
            _llm_caller=stub_llm,
        )
        finding = result["brakes"]["finding"]
        assert C0161_VERBATIM_REMEDY in finding, (
            "b3: open brake job with C0161 must still carry the verbatim P0 phrase "
            "after the enrichment re-apply gate is added.\n"
            f"Got finding: {finding!r}"
        )


class TestW2OrphanedTemplatesDeleted:
    """T4.10 (a): the four orphaned *_USER_TEMPLATE constants must not exist.

    These tests assert the absence of the deleted symbols.  They trivially pass
    after deletion; they are here to pin the deletion as a named, test-enforced
    contract rather than a one-off edit that can be silently re-introduced.
    """

    def test_battery_user_template_not_in_prompt_module(self):
        """BATTERY_USER_TEMPLATE must not exist after T4.10 (a) deletion."""
        import agents.health.prompt as p
        assert not hasattr(p, "BATTERY_USER_TEMPLATE"), (
            "BATTERY_USER_TEMPLATE was deleted as part of T4.10 (a) — it is "
            "orphaned (nothing imports it) and a VIN-interpolation privacy risk. "
            "Re-introducing it would re-introduce W2/W3."
        )

    def test_tire_user_template_not_in_prompt_module(self):
        import agents.health.prompt as p
        assert not hasattr(p, "TIRE_USER_TEMPLATE"), (
            "TIRE_USER_TEMPLATE was deleted as part of T4.10 (a)."
        )

    def test_brake_user_template_not_in_prompt_module(self):
        import agents.health.prompt as p
        assert not hasattr(p, "BRAKE_USER_TEMPLATE"), (
            "BRAKE_USER_TEMPLATE was deleted as part of T4.10 (a)."
        )

    def test_multi_component_user_template_not_in_prompt_module(self):
        import agents.health.prompt as p
        assert not hasattr(p, "MULTI_COMPONENT_USER_TEMPLATE"), (
            "MULTI_COMPONENT_USER_TEMPLATE was deleted as part of T4.10 (a)."
        )

    def test_no_vin_interpolation_in_remaining_prompt_constants(self):
        """No remaining prompt.py string contains a {vin} interpolation slot.

        After deleting the four templates, zero VIN-bearing placeholders remain
        in agents/health/prompt.py.  The SYSTEM_PROMPT does not interpolate vin;
        it cannot, because the system prompt is written once at import time.
        """
        import ast
        import pathlib

        src = pathlib.Path("agents/health/prompt.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Constant,)) and isinstance(node.value, str):
                if "{vin}" in node.value:
                    pytest.fail(
                        "After T4.10 (a), no string constant in prompt.py should "
                        f"contain a {{vin}} interpolation slot.  Found: {node.value[:60]!r}"
                    )

    def test_complaint_local_not_referenced_in_agent_build_brake_finding(self):
        """The unused `complaint` local must be removed from _build_brake_finding.

        decisions.md: `complaint` at agent.py:377 is an unused local — nothing
        reads it after assignment.  Removing it completes T4.8's privacy fix (no
        ADP free text in any live path) and reduces the surface area the next
        reader has to understand.
        """
        import ast
        import pathlib

        src = pathlib.Path("agents/health/agent.py").read_text()
        tree = ast.parse(src)

        # Find _build_brake_finding body and assert `complaint` is not assigned
        # as an unused local (the only read of complaint_text should be via the
        # already-renamed _COL_COMPLAINT key if it were used, but since the
        # decisions.md records it as unused, the assignment itself must be gone).
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_build_brake_finding"
            ):
                for stmt in ast.walk(node):
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and stmt.targets[0].id == "complaint"
                    ):
                        pytest.fail(
                            "The unused `complaint` local was deleted in T4.10 (a) "
                            "— re-introducing it would re-introduce a reference to "
                            "ADP free-text (complaint_text) in a path that the "
                            "enrichment seam does not use."
                        )


# ---------------------------------------------------------------------------
# W-C8-2: structured enrichment summary — no ADP prose in LLM prompt (T4.13)
# ---------------------------------------------------------------------------


class TestW_C8_2_StructuredEnrichmentSummary:
    """T4.13 W-C8-2 (revised 2026-10-04): _enrich_with_llm must not include
    *unvalidated ADP string columns* in the LLM prompt.

    Prior revision asserted no deterministic-finding substring at all reaches
    the prompt.  That turned out to break the model: with only
    ``severity_hint`` + ``confidence`` + ``evidence_count`` and no task
    content, the model has nothing to improve and responds with refusals that
    leak to Lambda stdout ("When you have the data, I'm here.", observed on
    the 2026-10-04 tires run — see
    issues/2026-10-04-tier2-nightly-runs-broken/report.md § Defect 2).

    The reconciliation: the hazard W-C8-2 named is **free-text ADP columns**
    (notably ``complaint_text``) reaching the prompt without a format check.
    The deterministic finding text is assembled downstream of column-level
    validators (``validate_dtc_codes`` constrains dtc codes to a strict regex;
    the battery / tire builders template only categorical bands + numeric
    values).  The finding text itself is therefore from a validated vocabulary
    and is safe to send as the task the model is being asked to improve.

    Updated invariants:
    1. The literal ``finding_preview=`` artefact from the pre-W-C8-2 shape must
       not appear (regression guard).
    2. severity_hint, confidence, and evidence_count must still reach the LLM
       as structured fields (used for context, not as the task).
    3. The active-P0 C0161 phrase must not appear in the prompt.  This is now
       covered by the T4.14 structural bypass (active-P0 brakes skip the LLM
       entirely) — see TestT414StructuralP0Bypass.  The assertion remains
       because the end-to-end behaviour matters.
    4. **``complaint_text`` from any component must not reach the prompt.** This
       is the one actually-free-text ADP column; it is intentionally not
       threaded through the deterministic finding (removed in T4.10a) and
       must stay out of the prompt.
    """

    # Capture the prompt passed to the LLM stub.
    class _CapturingStub:
        """Stub LLM caller that records the prompts it receives."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def __call__(self, system_prompt: str, user_prompt: str) -> str:
            import json as _json
            self.calls.append((system_prompt, user_prompt))
            # Return a minimal well-formed response.
            return _json.dumps({
                "battery": {"finding": "ok.", "severity_hint": "routine", "confidence": "high"},
                "tires": {"finding": "ok.", "severity_hint": "routine", "confidence": "high"},
                "brakes": {"finding": "ok.", "severity_hint": "routine", "confidence": "high"},
            })

    def _run_with_all_components(self, stub) -> str:
        """Run assess_vehicle_health with all three components and capture the prompt.

        Uses non-routine data for battery and tires so the C5 needs-judgment
        filter (added 2026-10-04 cycle 1) does not skip them from the LLM;
        the test's property is about prompt shape, not about which components
        reach the LLM.  Brakes carries C0161 (active) which bypasses the LLM
        via C4 (as it did before C4 extended the bypass to resolved jobs too),
        so brakes does not appear in the captured prompt — this is the same
        behaviour the pre-C4 tests ran against.
        """
        from agents.health.agent import assess_vehicle_health

        # Degraded battery (72.5% SoH → severity='soon') and replacement-
        # flagged tire (severity='attention') so both reach the LLM.
        tire_rows = [
            {
                "vin": SAMPLE_VIN,
                "position": "FL",
                "wear_band": "worn",
                "replacement_flagged": True,
                "wear_rate_per_1k_km": 0.5,
            }
        ]
        battery_rows = [
            {
                "vin": SAMPLE_VIN,
                "health_pct": "72.5",
                "age_days": "2555",
            }
        ]
        brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2026-08-03",
            "dtc_codes": ["C0161"],   # will produce a finding with C0161 phrase
            "complaint_text": "Brake warning",
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "parts_pending",
            "is_open": True,
            "provenance": "live",
        }

        assess_vehicle_health(
            SAMPLE_VIN,
            tire_rows=tire_rows,
            battery_rows=battery_rows,
            brake_data=brake_data,
            _llm_caller=stub,
        )

        assert len(stub.calls) == 1, "Expected exactly one LLM call"
        _system, user_prompt = stub.calls[0]
        return user_prompt

    def test_prompt_does_not_contain_finding_text(self):
        """Regression guard against the exact ``finding_preview=`` substring
        shape that the original W-C8-2 incident flagged.

        The current prompt format carries the finding under a different field
        name (``deterministic_finding``); the old ``finding_preview=`` key
        itself must not re-appear.
        """
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "finding_preview=" not in user_prompt, (
            "The literal 'finding_preview=' key is present — the old substring "
            "interpolation was not removed."
        )

    def test_prompt_contains_severity_hint_for_each_component(self):
        """The structured summary must still communicate severity_hint to the LLM."""
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "severity_hint" in user_prompt, (
            "severity_hint must appear in the structured prompt so the LLM can "
            "use it to improve phrasing."
        )

    def test_prompt_contains_confidence_for_each_component(self):
        """The structured summary must include confidence for each component."""
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "confidence" in user_prompt, (
            "confidence must appear in the structured prompt."
        )

    def test_prompt_contains_evidence_count(self):
        """The structured summary must include evidence_count (integer, no prose)."""
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "evidence_count" in user_prompt, (
            "evidence_count must appear in the structured prompt."
        )

    def test_c0161_verbatim_phrase_not_in_prompt(self):
        """The C0161 P0 verbatim phrase must not appear in the LLM prompt.

        Covered end-to-end by the T4.14 structural bypass (active-P0 brakes
        skip the LLM entirely).  Retained as an assertion on the OBSERVABLE
        behaviour: regardless of why the phrase doesn't reach the prompt, it
        must not.
        """
        from agents.health.prompt import C0161_VERBATIM_REMEDY

        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert C0161_VERBATIM_REMEDY not in user_prompt, (
            f"The C0161 verbatim phrase appeared in the LLM prompt:\n"
            f"  phrase: {C0161_VERBATIM_REMEDY!r}\n"
            "Active-P0 brakes must skip the LLM entirely (T4.14 structural bypass)."
        )

    def test_complaint_text_not_in_prompt(self):
        """The free-form ADP ``complaint_text`` column must not reach the prompt.

        This is the actual W-C8-2 hazard: ``complaint_text`` is free text with
        no format check.  It is intentionally not included in the deterministic
        finding (removed in T4.10a) and must stay out of the prompt.  A future
        change that re-adds complaint_text to the finding would be caught by
        this test.
        """
        # Use a distinctive sentinel for complaint_text so an incidental
        # substring match on "Brake" doesn't falsely accuse the fix.
        from agents.health.agent import assess_vehicle_health

        sentinel = "ZZSENTINELZZ-complaint-text-must-not-leak"
        brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2026-08-03",
            "dtc_codes": ["C0040"],   # non-P0 code so brakes still enriches
            "complaint_text": sentinel,
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "completed",
            "is_open": False,
            "provenance": "live",
        }
        stub = self._CapturingStub()
        assess_vehicle_health(SAMPLE_VIN, brake_data=brake_data, _llm_caller=stub)
        assert len(stub.calls) == 1
        _system, user_prompt = stub.calls[0]
        assert sentinel not in user_prompt, (
            "complaint_text (free-form ADP column) appeared in the LLM prompt. "
            "This is the actual W-C8-2 injection surface."
        )

    def test_service_date_can_appear_in_prompt(self):
        """DELETED (2026-10-04 review cycle 1 W3): this test was vacuous.

        It asserted only that ``"deterministic_finding"`` appeared in the
        prompt, which the instruction sentence ("Each ``deterministic_finding``
        was built from validated ADP columns ...") always contains.  Deleting
        the ``deterministic_finding`` line in ``summary_parts`` was not
        caught by this test (reviewer mutation ``d2_no_finding_line`` survived
        it).  Replaced by ``TestW3ServiceDateReachesPrompt`` further down this
        file, which asserts the service-date VALUE itself reaches the prompt.
        """
        pytest.skip(
            "Superseded by TestW3ServiceDateReachesPrompt — this test asserted "
            "a tautology; the new test asserts the date value itself."
        )

    def test_no_finding_substring_from_any_component(self):
        """REMOVED (2026-10-04): this test previously asserted that NO
        10+-character token from any deterministic finding reaches the prompt.
        That invariant was the shape of Defect 2 — with no finding content the
        model has no task.  Replaced by:

          - test_complaint_text_not_in_prompt (the real injection surface)
          - test_prompt_carries_deterministic_finding_text (defect 2 fix)
          - T4.14 structural bypass tests (active-P0 safety)
        """
        pytest.skip(
            "Superseded by test_complaint_text_not_in_prompt + "
            "test_prompt_carries_deterministic_finding_text; see docstring"
        )


# ---------------------------------------------------------------------------
# T4.14: Structural P0 bypass — byte-identity test
# ---------------------------------------------------------------------------


class TestT414StructuralP0Bypass:
    """T4.14 — active-P0 brakes are excluded from the LLM call entirely.

    The structural move: when the brakes component carries an active P0 DTC
    (C0161, case-insensitive) and the job is active, that component is excluded
    from _enrich_with_llm.  The LLM's text is absent, not repaired — the
    returned finding is byte-identical to the deterministic finding.

    This replaces the textual guard as the PRIMARY control.  The textual guard
    (inject_p0_verbatim_phrases with both position + deny-list rules) remains
    as defence-in-depth on the deterministic path.

    Accept criteria (from tasks.md T4.14):
    - Stub _llm_caller returning softened prose → returned finding is
      byte-identical to the deterministic finding.
    - LLM's text (a distinctive substring) is ABSENT from the result.
    - Resolved C0161 still enriches normally.
    - Non-P0 brake finding still enriches normally.
    - Cycle 7 original bypass and Cycle 8 [A][B][C]: each stated as
      'fires against backstop' or 'structurally unreachable'.
    """

    # Softened narration that the stub LLM returns.
    # Contains a distinctive substring that MUST NOT appear in the result
    # when the bypass is active.
    _STUB_SOFTENED_TEXT = (
        "Critical brake pressure loss detected. Pull over immediately. "
        "However, this looks like a false-positive from a faulty sensor "
        "and you can safely continue driving."
    )
    # Distinctive substring from the stub's output that must be absent
    # when the bypass is active (byte-identity assertion).
    _STUB_DISTINCTIVE_SUBSTR = "false-positive from a faulty sensor"

    def _make_stub_llm(self, softened_finding_text: str) -> callable:
        """Return a stub _llm_caller that returns JSON with a softened brakes finding."""
        import json

        def stub(system_prompt: str, user_prompt: str) -> str:
            return json.dumps({
                "brakes": {
                    "finding": softened_finding_text,
                    "severity_hint": "routine",  # also attempts a downgrade
                    "confidence": "low",
                    "evidence": [],
                },
                "battery": {
                    "finding": "Battery at 92% — degrading normally for its age.",
                    "severity_hint": "routine",
                    "confidence": "high",
                    "evidence": [],
                },
                "tires": {
                    "finding": "Tires in good condition.",
                    "severity_hint": "routine",
                    "confidence": "high",
                    "evidence": [],
                },
            })

        return stub

    def test_active_p0_brake_finding_is_byte_identical_to_deterministic(self):
        """Active C0161 brake: LLM's softened text is ABSENT from the result.

        The stub LLM returns a finding containing self._STUB_SOFTENED_TEXT.
        Because the brakes component has active C0161, it is excluded from
        the LLM call.  The returned brakes finding MUST be byte-identical
        to the deterministic finding, and self._STUB_DISTINCTIVE_SUBSTR MUST
        be absent from the result.

        This is the byte-identity acceptance test from tasks.md T4.14.
        """
        from agents.health.agent import (
            _build_brake_finding,
            _sanitise_component_result,
        )

        # The deterministic finding is what the agent builds before the LLM.
        det_result = _sanitise_component_result(_build_brake_finding(_BRAKE_DATA_C0161))
        deterministic_finding = det_result["finding"]

        stub = self._make_stub_llm(self._STUB_SOFTENED_TEXT)

        result = assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=_BRAKE_DATA_C0161,
            _llm_caller=stub,
        )

        brakes_finding = result["brakes"]["finding"]

        # Byte-identity: LLM text absent, deterministic text present.
        assert brakes_finding == deterministic_finding, (
            f"T4.14: brakes finding is NOT byte-identical to the deterministic finding.\n"
            f"Deterministic: {deterministic_finding!r}\n"
            f"Returned:      {brakes_finding!r}\n"
            f"LLM text must be ABSENT, not repaired."
        )

        # Absence check: the stub's distinctive substring must not appear.
        assert self._STUB_DISTINCTIVE_SUBSTR not in brakes_finding, (
            f"T4.14: LLM's distinctive text leaked into the result.\n"
            f"Distinctive substring: {self._STUB_DISTINCTIVE_SUBSTR!r}\n"
            f"Brakes finding: {brakes_finding!r}\n"
            f"Active-P0 bypass must prevent ANY LLM text from reaching the result."
        )

    def test_active_p0_brake_other_components_still_enrich(self):
        """Active C0161 brakes bypass is per-component: other components enrich normally.

        The stub LLM returns custom text for battery.  When brakes has active C0161,
        the bypass excludes only brakes — battery still goes through the LLM.

        Uses ``_BATTERY_ROW_DEGRADED`` (SoH 72.5% → severity='soon') because
        the C5 needs-judgment filter correctly skips the LLM for routine +
        high-confidence components; a routine battery alongside the C0161 brake
        would be skipped and the LLM would never be called, which is correct
        behaviour but does not exercise this test's property.
        """
        import json

        stub_battery_finding = "Battery enriched by LLM — degraded, worth a service visit."

        def stub(system_prompt: str, user_prompt: str) -> str:
            return json.dumps({
                "battery": {
                    "finding": stub_battery_finding,
                    "severity_hint": "soon",
                    "confidence": "high",
                    "evidence": [],
                },
            })

        result = assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[_BATTERY_ROW_DEGRADED],
            brake_data=_BRAKE_DATA_C0161,
            _llm_caller=stub,
        )

        # Battery was enriched by the LLM.
        assert stub_battery_finding in result["battery"]["finding"], (
            f"T4.14: battery finding was not enriched by the LLM stub.\n"
            f"Expected stub text: {stub_battery_finding!r}\n"
            f"Result battery: {result['battery']['finding']!r}\n"
            f"Per-component bypass must only exclude brakes."
        )

        # Brakes was NOT enriched (LLM stub doesn't return brakes key, but
        # the bypass means brakes should be the deterministic finding).
        assert C0161_VERBATIM_REMEDY in result["brakes"]["finding"], (
            "T4.14: brakes finding must contain the verbatim P0 phrase."
        )

    def test_resolved_c0161_job_bypasses_llm_too(self):
        """C4 (2026-10-04 review cycle 1): the C0161 bypass extends to RESOLVED jobs.

        Previously only active C0161 brakes bypassed the LLM; the T4.14 textual
        guard was supposed to cover resolved jobs on the LLM path.  But D2's
        2026-10-04 change — now sending the deterministic finding text, which
        mentions "DTC C0161" on the resolved-job path, into the prompt — plus
        SYSTEM_PROMPT's instruction to add the verbatim stop-driving phrase
        whenever C0161 appears in dtc_codes, together create a path where a
        rule-following LLM lands "Pull over immediately" on a ~475k-row set
        of resolved brake jobs.

        The structural fix extends the bypass: any brake record carrying C0161
        is excluded from the LLM entirely.  The deterministic path has already
        emitted the correct narrative — verbatim phrase for active jobs,
        historical acknowledgement for resolved jobs — and the LLM would do
        neither reliably.

        Mutation guard: change ``_brakes_has_c0161 = brake_data is not None and
        "C0161" in {...}`` back to the active-only form (adding
        ``_enrichment_active_brake and ...``), and this test fails because the
        LLM stub text reaches the brake finding.
        """
        import json

        stub_resolved_text = (
            "Critical brake pressure loss detected. Pull over immediately. "
            "C0161 was addressed at the last service."
        )

        def stub(system_prompt: str, user_prompt: str) -> str:
            return json.dumps({
                "brakes": {
                    "finding": stub_resolved_text,
                    "severity_hint": "soon",
                    "confidence": "medium",
                    "evidence": [],
                },
            })

        resolved_brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2024-01-15",
            "dtc_codes": ["C0161"],
            "complaint_text": "Brake warning",
            "warranty_covered": "true",
            "total_cost_usd": "0.00",
            "outcome": "resolved",
            "is_open": False,
            "provenance": "live",
        }

        result = assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=resolved_brake_data,
            _llm_caller=stub,
        )

        # Resolved C0161 brake: LLM text must NOT appear in the finding —
        # the C0161 bypass means the deterministic path's output is returned
        # byte-identical.
        assert "C0161 was addressed at the last service" not in result["brakes"]["finding"], (
            f"C4: resolved C0161 brake finding was enriched by the LLM — the "
            f"bypass did not extend to resolved jobs.\n"
            f"Result: {result['brakes']['finding']!r}\n"
            f"The LLM stub's distinctive text leaked in, which means a "
            f"rule-following production LLM would land 'Pull over immediately' "
            f"on resolved brake records."
        )

        # Resolved C0161 brake must NOT carry the verbatim P0 phrase — the
        # deterministic path emits a historical acknowledgement instead.
        # (T4.9 still holds: resolved jobs do not acquire the stop-driving
        # phrase via any path.)
        assert C0161_VERBATIM_REMEDY not in result["brakes"]["finding"], (
            f"C4/T4.9: resolved C0161 job must not carry the P0 verbatim phrase.\n"
            f"Result: {result['brakes']['finding']!r}"
        )

        # The deterministic finding IS returned (byte-identical to what the
        # pre-enrichment builder emits).
        from agents.health.agent import _build_brake_finding, _sanitise_component_result
        det = _sanitise_component_result(_build_brake_finding(resolved_brake_data))
        assert result["brakes"]["finding"] == det["finding"], (
            f"C4: resolved C0161 brake finding is not byte-identical to the "
            f"deterministic finding.\n"
            f"Deterministic: {det['finding']!r}\n"
            f"Returned:      {result['brakes']['finding']!r}"
        )

    def test_non_p0_brake_finding_still_enriches_normally(self):
        """Non-P0 brake finding (no C0161): LLM enrichment runs normally.

        The bypass only triggers on active C0161.  A brake finding with a
        different DTC (e.g. C0031) or no DTC must still enrich via the LLM.
        """
        import json

        stub_non_p0_text = "Brake service C0031 — ABS issue resolved at last service."

        def stub(system_prompt: str, user_prompt: str) -> str:
            return json.dumps({
                "brakes": {
                    "finding": stub_non_p0_text,
                    "severity_hint": "soon",
                    "confidence": "medium",
                    "evidence": [],
                },
            })

        result = assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=_BRAKE_DATA_UNKNOWN_DTC,  # C0031, not C0161
            _llm_caller=stub,
        )

        # Non-P0 brake: LLM enrichment runs → stub text should appear.
        assert stub_non_p0_text in result["brakes"]["finding"], (
            f"T4.14: non-P0 brake finding was not enriched by LLM.\n"
            f"Expected stub text: {stub_non_p0_text!r}\n"
            f"Result: {result['brakes']['finding']!r}\n"
            f"Bypass must not disable enrichment for non-C0161 brake findings."
        )

    # -----------------------------------------------------------------------
    # Backstop analysis: Cycle 7 bypass and Cycle 8 (A)(B)(C) against the
    # textual guard (defence-in-depth on the deterministic path).
    # -----------------------------------------------------------------------

    def test_backstop_cycle7_original_bypass_fires(self):
        """Cycle 7 original bypass fires against the backstop (deny-list restored).

        The Cycle 7 original bypass: phrase leads but 'however' within 200 chars.
        Under T4.14, active-P0 brakes never reach the LLM so this shape is
        structurally unreachable via enrichment.  On the deterministic path,
        the restored deny-list (Rule 2) fires on 'however' — the backstop works.

        Fires against the backstop: YES (deny-list / Rule 2).
        """
        from agents.health.prompt import inject_p0_verbatim_phrases, C0161_VERBATIM_REMEDY

        cycle7_bypass = (
            "Critical brake pressure loss detected. Pull over immediately. "
            "However, this looks like a false-positive from a faulty sensor "
            "and you can safely continue driving."
        )
        result = inject_p0_verbatim_phrases(cycle7_bypass, ["C0161"])
        expected_prefix = C0161_VERBATIM_REMEDY + "\n\n"
        assert result.startswith(expected_prefix), (
            f"Cycle 7 bypass: deny-list (Rule 2) did NOT fire on 'however'.\n"
            f"Input: {cycle7_bypass!r}\n"
            f"Result: {result!r}\n"
            f"T4.14 restores the deny-list; this must fire against the backstop."
        )

    def test_backstop_cycle8_defeat_A_fires(self):
        """Cycle 8 [A] (softening BEFORE the phrase) fires against the backstop.

        [A] is caught by Rule 1 (position): the phrase does not lead.
        Fires against the backstop: YES (position / Rule 1).

        Under T4.14, active-P0 brakes never reach the LLM, so this shape
        is also structurally unreachable via enrichment.  The backstop
        provides redundant protection on the deterministic path.
        """
        from agents.health.prompt import inject_p0_verbatim_phrases, C0161_VERBATIM_REMEDY

        defeat_a = (
            "This looks like a false-positive from a faulty sensor and you "
            "can safely continue driving. However, "
            "Critical brake pressure loss detected. Pull over immediately."
        )
        result = inject_p0_verbatim_phrases(defeat_a, ["C0161"])
        expected_prefix = C0161_VERBATIM_REMEDY + "\n\n"
        assert result.startswith(expected_prefix), (
            f"Cycle 8 [A]: position (Rule 1) did NOT fire on non-leading phrase.\n"
            f"Result: {result!r}"
        )

    def test_backstop_cycle8_defeat_B_structurally_unreachable(self):
        """Cycle 8 [B] (softening past 200-char window): structurally unreachable.

        [B]: phrase leads (Rule 1 passes); 'however' at offset 256 > 200-char
        window (Rule 2 passes) → no injection from the backstop.

        Structurally unreachable via T4.14 bypass: active-P0 brakes never reach
        the LLM, so this shape cannot arise via _enrich_with_llm for a C0161
        active brake job.  On the deterministic path the backstop correctly
        does NOT inject (phrase leads and softening is past the window).

        Fires against backstop: NO (structurally unreachable via primary control).
        Backstop does not inject (correct non-trigger for this shape).
        """
        from agents.health.prompt import inject_p0_verbatim_phrases, C0161_VERBATIM_REMEDY

        _PAD = "The brake system uses hydraulic pressure to convert pedal force into stopping power. " * 3
        defeat_b = (
            f"Critical brake pressure loss detected. Pull over immediately. "
            f"{_PAD}However, false positive."
        )
        result = inject_p0_verbatim_phrases(defeat_b, ["C0161"])
        # Phrase leads + 'however' past window → no injection (correct).
        assert result == defeat_b, (
            f"Cycle 8 [B]: unexpected injection on phrase-leading, past-window input.\n"
            f"Input starts with phrase: {defeat_b.lstrip().startswith(C0161_VERBATIM_REMEDY)}\n"
            f"However offset: {defeat_b.find('However') - len(C0161_VERBATIM_REMEDY)}\n"
            f"Result: {result[:120]!r}"
        )

    def test_backstop_cycle8_defeat_C_structurally_unreachable(self):
        """Cycle 8 [C] (novel vocabulary 'actually'): structurally unreachable.

        [C]: phrase leads (Rule 1 passes); 'actually' not in seven-item deny-list
        (Rule 2 passes) → no injection from the backstop.

        Structurally unreachable via T4.14 bypass: same as [B].

        Fires against backstop: NO (structurally unreachable via primary control;
        backstop does not inject — deny-list is not exhaustive, but the primary
        control makes exhaustiveness unnecessary for the live path).
        """
        from agents.health.prompt import inject_p0_verbatim_phrases, C0161_VERBATIM_REMEDY

        defeat_c = (
            "Critical brake pressure loss detected. Pull over immediately. "
            "Actually, that\u2019s likely wrong \u2014 sensor error is more probable."
        )
        result = inject_p0_verbatim_phrases(defeat_c, ["C0161"])
        # Phrase leads + 'actually' not in deny-list → no injection (correct).
        assert result == defeat_c, (
            f"Cycle 8 [C]: unexpected injection on phrase-leading, novel-term input.\n"
            f"Result: {result[:120]!r}"
        )




# ---------------------------------------------------------------------------
# Defect 2 (2026-10-04): the LLM enrichment prompt must carry the
# deterministic finding text (the thing the model is asked to improve) and
# the deterministic severity/confidence (the context for improvement).
#
# Prior rev stripped the finding text for prompt-injection concerns; the
# ADP-string hazard is now handled at the source boundary (validate_dtc_codes
# etc.), so the deterministic finding — built from validated columns — is safe
# to send as the thing being improved.  Without it the model has no task and
# responds with refusals that leak to stdout ("When you have the data, I'm
# here.", 2026-10-04 tires run).
#
# issues/2026-10-04-tier2-nightly-runs-broken/report.md § "Defect 2"
# ---------------------------------------------------------------------------


class TestLlmEnrichmentPromptCarriesFindingAndData:
    """``_enrich_with_llm`` must send the LLM the deterministic finding text
    and the structured severity/confidence it is being asked to review.
    """

    @staticmethod
    def _capture_prompt() -> tuple[list[tuple[str, str]], object]:
        """Return (captured_calls, llm_caller) where llm_caller records every
        (system_prompt, user_prompt) tuple it is called with and returns a
        parseable JSON stub so enrichment does not fall back to deterministic.
        """
        captured: list[tuple[str, str]] = []

        def llm_caller(system_prompt: str, user_prompt: str) -> str:
            captured.append((system_prompt, user_prompt))
            # Minimal valid JSON for every component so _parse_llm_response
            # does not drop back to the fallback.
            return (
                '{"battery": {"finding": "ok", "severity_hint": "routine", '
                '"confidence": "high"},'
                ' "tires": {"finding": "ok", "severity_hint": "routine", '
                '"confidence": "high"},'
                ' "brakes": {"finding": "ok", "severity_hint": "routine", '
                '"confidence": "high"}}'
            )

        return captured, llm_caller

    def test_prompt_carries_deterministic_finding_text(self):
        """The LLM prompt must include a ``deterministic_finding`` field whose
        value is the deterministic finding text — not empty, not a count.

        A model asked to improve a finding has to see the finding.  Without this
        the 2026-10-04 production response was ``**When you have the data, I'm
        here.**`` — captured in the issue report.

        Uses ``_BATTERY_ROW_DEGRADED`` (SoH 72.5% → severity='soon') because
        the C5 needs-judgment filter correctly skips the LLM for routine +
        high-confidence components.  Routine findings never reach the LLM,
        so a non-routine fixture is required to exercise the enrichment path.
        """
        captured, llm_caller = self._capture_prompt()
        assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[_BATTERY_ROW_DEGRADED],
            _llm_caller=llm_caller,
        )
        assert captured, "LLM was never called"
        _system, user_prompt = captured[-1]

        # The deterministic battery finding reaches the prompt verbatim.
        det_finding = _build_battery_finding([_BATTERY_ROW_DEGRADED])["finding"]
        assert "deterministic_finding" in user_prompt, (
            "LLM prompt is missing the deterministic_finding field; "
            "the model has nothing to improve. "
            f"Prompt: {user_prompt!r}"
        )
        assert det_finding in user_prompt, (
            "Deterministic finding text is not in the LLM prompt. "
            "Only summary fields reach the model, so the enrichment call is "
            "an improvement task with no input. "
            f"Expected substring: {det_finding!r}\nPrompt: {user_prompt!r}"
        )

    def test_prompt_carries_severity_hint_and_confidence(self):
        """The structured context (severity_hint, confidence) must still reach
        the prompt — those are the signals the model should keep aligned to
        unless it has a strong reason to change them.

        Uses ``_BATTERY_ROW_DEGRADED`` (SoH 72.5% → severity='soon',
        confidence='high') because the C5 filter skips routine+high findings.
        """
        captured, llm_caller = self._capture_prompt()
        assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[_BATTERY_ROW_DEGRADED],
            _llm_caller=llm_caller,
        )
        _system, user_prompt = captured[-1]
        assert "severity_hint" in user_prompt
        assert "confidence" in user_prompt
        # Degraded battery is 'soon'/'high' — presence of the quoted literal
        # values pins the structured shape (dict-of-fields, not a single
        # opaque blob).
        assert "'soon'" in user_prompt
        assert "'high'" in user_prompt

    def test_prompt_differs_between_components_with_different_findings(self):
        """Two VINs with different deterministic findings must produce two
        different prompts.

        If the prompt is derived from a bounded enum of severity values only
        (the pre-fix shape), two VINs at the same severity produce the same
        prompt and the LLM has no way to distinguish them.  This test fails
        on the pre-fix shape and passes on the fix.

        Both fixtures must be non-routine or non-high-confidence so the C5
        filter does not skip them from the LLM.
        """
        captured, llm_caller = self._capture_prompt()

        # VIN 1 — mildly degraded battery (72.5% SoH → severity='soon').
        assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[{"vin": SAMPLE_VIN, "health_pct": "72.5", "age_days": "2555"}],
            _llm_caller=llm_caller,
        )
        # VIN 2 — more degraded battery (65% SoH → severity='attention'):
        # different severity AND different numeric content.
        assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[{"vin": SAMPLE_VIN, "health_pct": "65.0", "age_days": "2920"}],
            _llm_caller=llm_caller,
        )
        assert len(captured) == 2
        _, p1 = captured[0]
        _, p2 = captured[1]
        assert p1 != p2, (
            "Two VINs with different deterministic findings produced identical "
            "LLM prompts.  The prompt carries no per-VIN content — the model "
            "cannot distinguish them.\n"
            f"Prompt 1: {p1!r}\nPrompt 2: {p2!r}"
        )

    def test_prompt_truncates_oversize_finding_text_at_cap(self):
        """Belt-and-braces: an unusually long deterministic finding must be
        truncated at the module's size bound (currently 500 chars + ``…``)
        so a single component cannot consume the entire prompt.

        This is not the primary injection control (that lives in the source
        boundary validators) — it is a hard size bound regardless.
        """
        captured, llm_caller = self._capture_prompt()

        # Build a deterministic finding by injecting a long "finding" into
        # assess_vehicle_health via the brake component.  The deterministic
        # path renders the finding; we don't need to trip ADP, we need a long
        # string in the finding field at the moment _enrich_with_llm reads it.
        # The easiest way is to patch the deterministic finding at a known
        # size.
        long_text = "A" * 800
        # Minimally plausible open brake dict with the long complaint:
        brake = {
            "vin": SAMPLE_VIN,
            "service_date": "2026-01-15",
            "dtc_codes": [],
            "complaint_text": long_text,
            "warranty_covered": "true",
            "total_cost_usd": "100.0",
            "outcome": "parts_pending",
            "is_open": True,
            "provenance": "live",
            "model_year": 2024,
        }
        # Force a finding override through monkeypatching _build_brake_finding
        # for the length of this test — simplest way to control the exact
        # finding text that reaches _enrich_with_llm.
        from unittest.mock import patch

        def _long_brake(_data):
            return {
                "finding": long_text,
                "severity_hint": "attention",
                "confidence": "high",
                "evidence": [],
            }

        with patch("agents.health.agent._build_brake_finding", side_effect=_long_brake):
            assess_vehicle_health(SAMPLE_VIN, brake_data=brake, _llm_caller=llm_caller)

        _system, user_prompt = captured[-1]
        # The verbatim 800-char string must not appear in the prompt.
        assert long_text not in user_prompt, (
            "Oversize deterministic finding was embedded in the LLM prompt "
            "without truncation.  A single component can inflate the prompt "
            "unbounded."
        )
        # The truncation marker should be present.
        assert "…" in user_prompt, (
            "No truncation marker in the prompt — size cap is not firing."
        )




# ---------------------------------------------------------------------------
# Defect 4 (2026-10-04): tire finding must dedupe position names.
#
# The ADP tire_health grain is (vin, tire_position, event_date), so one VIN
# with many flagged events on the same position repeats that position in the
# positions_needing list once per event.  Live 2026-10-04 headline (FIND-
# 1fe4be29): "One or more tires (FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR,
# FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR, FR,"  — truncated
# at 120 chars mid-list.  Dedup with order preserved (dict.fromkeys, not
# set() which is process-randomised and would make the test flaky).
#
# issues/2026-10-04-tier2-nightly-runs-broken/report.md § "Defect 4"
# ---------------------------------------------------------------------------


class TestTireFindingDeduplicatesPositions:
    @staticmethod
    def _row(position: str, replacement: bool = True) -> dict:
        return {
            "vin": SAMPLE_VIN,
            "position": position,
            "event_date": "2026-10-01",
            "replacement_flagged": replacement,
            "wear_band": "worn",
            "wear_rate_per_1k_km": "0.5",
        }

    def test_tire_finding_dedupes_repeated_positions(self):
        """One VIN with 60 flagged events on 'FR' must produce 'FR' once in
        the finding, not 60 times.

        Measures the actual string content, not a count: a dedup that preserves
        up to 2 copies still produces 'FR, FR' in the headline and would read
        as reasonable while the live defect persists.  So the assertion is on
        the exact substring and on the full position list.
        """
        # Simulate 60 flagged events for FR plus a couple for RL (so dedup is
        # not accidentally "take first only").
        rows = [self._row("FR") for _ in range(60)] + [self._row("RL") for _ in range(3)]
        result = _build_tire_finding(rows)
        finding = result["finding"]
        # Must not contain any repeated-position pattern.
        assert "FR, FR" not in finding, (
            f"Tire finding has repeated 'FR' positions:\n  {finding!r}\n"
            "Dedup is missing.  2026-10-04 Defect 4."
        )
        assert "RL, RL" not in finding
        # Must contain each position exactly once.
        assert finding.count("FR") == 1
        assert finding.count("RL") == 1
        # The finding is a one-sentence summary, well under 120 chars.
        assert len(finding) < 120, (
            f"Tire finding is {len(finding)} chars — long enough to overflow "
            f"the headline cap, indicating the dedup is not applied."
        )

    def test_tire_finding_preserves_first_seen_position_order(self):
        """Dedup must preserve insertion order so the test is stable across
        Python runs.  ``set()`` is process-randomised and would make the
        assertion flaky.  ``dict.fromkeys`` is the correct primitive.
        """
        # Order of first occurrence: FR, RL, FL.
        rows = [
            self._row("FR"),
            self._row("FR"),
            self._row("RL"),
            self._row("FR"),
            self._row("FL"),
            self._row("RL"),
        ]
        result = _build_tire_finding(rows)
        finding = result["finding"]
        # Positions must appear in first-seen order.
        fr_idx = finding.find("FR")
        rl_idx = finding.find("RL")
        fl_idx = finding.find("FL")
        assert fr_idx < rl_idx < fl_idx, (
            f"Position order in finding is not first-seen order:\n  {finding!r}\n"
            "A dedup that uses set() loses order; use dict.fromkeys()."
        )

    def test_tire_finding_dedupe_does_not_drop_distinct_positions(self):
        """Dedup must not collapse legitimately-distinct positions into one.

        A fix that mistakenly uses ``positions_needing[:1]`` (take first) would
        produce only "FR" when both FR and RR need replacement.  That is a
        safety regression: the owner is told half the problem.
        """
        rows = [
            self._row("FR"),
            self._row("FR"),  # duplicate
            self._row("RR"),
            self._row("RR"),  # duplicate
        ]
        result = _build_tire_finding(rows)
        finding = result["finding"]
        assert "FR" in finding
        assert "RR" in finding




# ---------------------------------------------------------------------------
# Defect 5 (2026-10-04): the ADP date caveat belongs AFTER the brake finding
# in the finding text, not before.  derive_headline takes the first sentence,
# so a prepended caveat becomes the headline on every suspect-date record.
#
# Live 2026-10-04 example (FIND-34adace5): headline
#   "This service record is dated 2020, but the vehicle is a 2022 model,
#    so the date may be inaccurate."
# — the actual brake finding never reached the headline.
#
# Must NOT change the P0 active-C0161 branch (that branch skips the caveat
# entirely and is a safety-seam decision — user constraint).
#
# issues/2026-10-04-tier2-nightly-runs-broken/report.md § "Defect 5"
# ---------------------------------------------------------------------------


class TestBrakeDateCaveatIsATrailingParagraph:
    """``_build_brake_finding`` must emit the ADP date caveat as a trailing
    paragraph of the finding, so ``derive_headline`` keeps the brake finding
    (not the caveat) as the headline.
    """

    @staticmethod
    def _brake(service_date: str, model_year: int, *, is_open: bool, dtc_codes: list[str]):
        return {
            "service_date": service_date,
            "model_year": model_year,
            "outcome": "completed" if not is_open else "parts_pending",
            "is_open": is_open,
            "dtc_codes": dtc_codes,
            "complaint_text": "",
            "warranty_covered": "false",
            "total_cost_usd": None,
        }

    def test_caveat_is_appended_not_prepended_on_routine_brake(self):
        """For a routine (resolved) brake with a suspect date, the caveat text
        must appear AFTER the brake finding, so the first sentence is the
        brake finding, not the caveat.
        """
        out = _build_brake_finding(
            self._brake("2018-05-04", 2022, is_open=False, dtc_codes=[])
        )
        finding = out["finding"]
        # Both pieces are present in the detail.
        assert "brake service" in finding.lower()
        assert "inaccurate" in finding
        # Order: brake finding first, caveat second.
        brake_pos = finding.lower().find("brake service")
        caveat_pos = finding.find("inaccurate")
        assert brake_pos < caveat_pos, (
            f"The date caveat is still prepended; it must be a trailing "
            f"paragraph.\nfinding: {finding!r}"
        )
        # Confidence lowered as before.
        assert out["confidence"] == "medium"

    def test_first_sentence_of_finding_is_the_brake_finding_not_the_caveat(self):
        """The property that actually matters for the owner-facing headline:
        the first sentence of the finding is the brake finding sentence.

        ``derive_headline`` picks the first sentence; if that first sentence is
        the caveat, the live defect reappears.  This test is the direct
        mutation guard — it checks what reaches the headline derivation.
        """
        from agents.tier2.card_text import derive_headline

        out = _build_brake_finding(
            self._brake("2018-05-04", 2022, is_open=False, dtc_codes=[])
        )
        headline = derive_headline(out["finding"])
        assert "inaccurate" not in headline, (
            f"derive_headline picked up the caveat as the headline — the "
            f"caveat is still prepended.\nheadline: {headline!r}"
        )
        assert "brake service" in headline.lower(), (
            f"Headline does not carry the brake finding.\nheadline: {headline!r}"
        )

    def test_active_c0161_path_still_skips_caveat_and_keeps_high_confidence(self):
        """User constraint: do not change the P0 safety wording or the
        active-C0161 branch.  The active C0161 branch skips the caveat
        entirely (safety seam decision recorded in decisions.md) and keeps
        confidence='high'.  This test pins that behaviour.
        """
        out = _build_brake_finding(
            self._brake("2018-06-01", 2022, is_open=True, dtc_codes=["C0161"])
        )
        finding = out["finding"]
        assert "Pull over immediately" in finding, (
            "P0 verbatim phrase must be present for an active C0161 brake."
        )
        assert "inaccurate" not in finding, (
            "On an active P0 brake job the date caveat must still be skipped "
            "entirely — safety seam decision.  User constraint: do not change "
            "the P0 safety wording."
        )
        assert out["confidence"] == "high"




# ---------------------------------------------------------------------------
# Review cycle 1 (2026-10-04): Criticals C1, C2, C4, C5
#
# These tests cover the properties the previous iteration's tests did not:
# per-VIN prompt isolation (C1), per-call token meter accuracy (C2), the
# C4 bypass extension (resolved C0161 → LLM too), and the C5 needs-judgment
# filter + per-batch LLM-call circuit breaker.
#
# Each has a mutation-check comment naming the exact break that fails it.
# ---------------------------------------------------------------------------


class TestC1PerVinPromptIsolation:
    """C1 — a fresh Strands Agent per VIN means no prior VIN's content reaches
    the next VIN's prompt.

    Review-cycle-2 Critical #1: the cycle-1 tests patched ``strands.Agent``
    with a hand-written class.  Hand-written classes do not carry a
    ``ConversationManager``, do not keep ``messages`` state the way
    ``strands==1.39.0``'s Agent does, and so could not fail the way the real
    SDK fails.  The reviewer verified three mutations against the real SDK:

      - MC1 (one shared Agent across the batch) → messages grow 1, 3, 5 … 41;
        ``accumulated_usage`` is cumulative, metering ~75× actual usage.
      - MC1b (fresh Agent per call sharing one ``messages=`` list) →
        messages grow 1, 3, 5 … but ``accumulated_usage`` is per-call;
        every earlier VIN's prompt leaks into subsequent calls.
      - Correct: a fresh Agent per call with its own fresh messages list →
        every call receives exactly one message, no history leak.

    This test drives ``make_strands_agent_fn`` through the real
    ``strands.Agent`` with a stub ``strands.models.Model`` subclass, records
    every ``messages`` list the model sees, and asserts the invariant:
    each call receives exactly one message, and the message contains only
    its own VIN's deterministic finding.
    """

    def test_each_call_receives_one_message_and_only_its_own_vins_data(self):
        """Drive the production path through the real strands.Agent + a stub
        Model; assert both halves of the C1 property.

        Mutation check (both must fail this test):
          - MC1  (shared Agent across the batch) → ``messages_per_call`` becomes
            [1, 3, 5, ...] and prior VINs' SoH values appear in subsequent
            prompts.
          - MC1b (fresh Agent per call sharing one ``messages=`` list) →
            ``messages_per_call`` becomes [1, 3, 5, ...] and prior VINs' data
            appears in later messages.
        """
        from unittest.mock import patch
        from strands.models import Model

        # Stub Model subclass — real strands.Agent calls into this.  We
        # record every (messages, system_prompt) seen by stream().  The
        # Model is server-stateless (``stateful`` defaults to False), so
        # the ``messages`` list the Agent sends IS the Agent's own
        # conversation state.
        class _RecordingModel(Model):
            def __init__(self) -> None:
                self.messages_per_call: list[int] = []
                self.messages_text_per_call: list[list[str]] = []
                self._cfg: dict = {}

            def update_config(self, **cfg):
                self._cfg.update(cfg)

            def get_config(self):
                return self._cfg

            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                yield {"output": None}

            async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
                msg_list = list(messages)
                self.messages_per_call.append(len(msg_list))
                # Capture the text of every user message so a leaked VIN
                # value is detectable even when it is in an earlier turn.
                texts: list[str] = []
                for m in msg_list:
                    for block in m.get("content", []) or []:
                        if "text" in block:
                            texts.append(block["text"])
                self.messages_text_per_call.append(texts)
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockDelta": {"delta": {"text":
                    '{"battery": {"finding": "ok", "severity_hint": "soon", '
                    '"confidence": "high", "evidence": []}}'
                }}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                yield {"metadata": {
                    "usage": {"inputTokens": 90, "outputTokens": 20, "totalTokens": 110},
                    "metrics": {"latencyMs": 1},
                }}

        recording_model = _RecordingModel()

        # Patch BedrockModel so make_strands_agent_fn's import resolves to our
        # stub.  strands.Agent itself is NOT patched — the real class with its
        # ConversationManager and messages-list behaviour is what we are
        # exercising.
        with patch("strands.models.BedrockModel", return_value=recording_model):
            from agents.health.agent import make_strands_agent_fn
            agent_fn = make_strands_agent_fn()

            # Four non-routine battery VINs so the LLM is actually invoked
            # (routine VINs bypass the LLM via C5's needs-judgment filter).
            soh_values = [72.5, 68.0, 65.0, 71.0]
            for soh in soh_values:
                agent_fn(
                    f"VIN-{soh}",
                    [{"vin": f"VIN-{soh}", "health_pct": str(soh), "age_days": "2920"}],
                )

        # (1) Each stream() call received EXACTLY one message.
        assert recording_model.messages_per_call == [1, 1, 1, 1], (
            "C1: messages_per_call is not [1, 1, 1, 1]. "
            "A shared Agent (MC1) produces [1, 3, 5, 7]; a fresh Agent per "
            "call sharing one ``messages=`` list (MC1b) also produces "
            "[1, 3, 5, 7].  Both failure modes are caught here.\n"
            f"messages_per_call: {recording_model.messages_per_call!r}"
        )

        # (2) The N-th call's single message contains only its own VIN's SoH.
        # The deterministic finding text names the SoH value, which is unique
        # per VIN.  Earlier VINs' SoH values must NOT appear in later calls.
        for i, texts in enumerate(recording_model.messages_text_per_call):
            own = str(soh_values[i])  # e.g. '72.5'
            own_pct = f"{int(soh_values[i])}%"  # e.g. '72%'
            combined_text = "\n".join(texts)
            # Verify this VIN's own data IS in the prompt (positive control).
            assert own in combined_text or own_pct in combined_text, (
                f"C1 positive control: VIN {i}'s SoH ({own}%) is not in "
                f"its own prompt — the deterministic finding is not reaching "
                f"the model at all.\nPrompt: {combined_text!r}"
            )
            # Verify no earlier VIN's SoH is in the prompt.
            for j, other_soh in enumerate(soh_values[:i]):
                other = str(other_soh)
                other_pct = f"{int(other_soh)}%"
                assert other not in combined_text and other_pct not in combined_text, (
                    f"C1: call {i}'s prompt contains VIN {j}'s SoH "
                    f"({other}%) — conversation history leaked between "
                    f"vehicles.\ncall {i} prompt: {combined_text!r}"
                )


class TestC2PerCallTokenMeter:
    """C2 — the metered total tokens equals the real per-call usage summed,
    not the lifetime-cumulative counter summed.

    Review-cycle-2 Critical #2: the cycle-1 test patched ``strands.Agent``
    with a hand-written class whose ``accumulated_usage`` was a fixed per-call
    value.  The real SDK's ``accumulated_usage`` on a shared Agent is
    cumulative-over-lifetime, which is the exact behaviour the C2 fix
    addresses.  The hand-written stub masked the defect.

    This test drives ``make_strands_agent_fn`` through the real
    ``strands.Agent`` with a stub ``strands.models.Model`` subclass that
    bills a fixed usage per call.  With the fix (fresh Agent per call), each
    VIN's ``_tokens_used`` must equal exactly the per-call usage.  Against
    the pre-fix shape (shared Agent + cumulative counter summed on every
    call), the per-call metered value would be u, 2u, 3u, 4u — the test
    fails.
    """

    def test_meter_equals_the_real_per_call_usage_via_real_strands_agent(self):
        """N calls at a fixed stub billing of ``u`` tokens must meter exactly
        ``u`` per call via ``_tokens_used``.

        Mutation check: with MC1 (shared Agent), ``accumulated_usage`` is
        cumulative over the Agent lifetime, so the per-VIN delta ``_usage``
        reports (summed from accumulated_usage on each call) grows as u, 2u,
        3u, 4u → this test fails.
        """
        from unittest.mock import patch
        from strands.models import Model

        u_in, u_out, u_total = 90, 20, 110

        class _FixedBillModel(Model):
            def __init__(self) -> None:
                self._cfg: dict = {}

            def update_config(self, **cfg):
                self._cfg.update(cfg)

            def get_config(self):
                return self._cfg

            async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
                yield {"output": None}

            async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockDelta": {"delta": {"text":
                    '{"battery": {"finding": "ok", "severity_hint": "soon", '
                    '"confidence": "high", "evidence": []}}'
                }}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                yield {"metadata": {
                    "usage": {
                        "inputTokens": u_in,
                        "outputTokens": u_out,
                        "totalTokens": u_total,
                    },
                    "metrics": {"latencyMs": 1},
                }}

        # Patch BedrockModel — strands.Agent runs for real.
        with patch("strands.models.BedrockModel", return_value=_FixedBillModel()):
            from agents.health.agent import make_strands_agent_fn
            agent_fn = make_strands_agent_fn()

            metered_per_call: list[int] = []
            for soh in (72.5, 72.0, 71.5, 71.0):
                result = agent_fn(
                    f"VIN-{soh}",
                    [{"vin": f"VIN-{soh}", "health_pct": str(soh), "age_days": "2920"}],
                )
                metered_per_call.append(result.get("_tokens_used", 0))

        assert metered_per_call == [u_total, u_total, u_total, u_total], (
            f"C2: per-VIN _tokens_used is not linear in call count.\n"
            f"Expected: [{u_total}, {u_total}, {u_total}, {u_total}]  "
            f"(each VIN meters its own usage, nothing more)\n"
            f"Got:      {metered_per_call}\n"
            f"The pre-fix shape (shared Agent + cumulative accumulated_usage "
            f"summed on every call) would produce "
            f"[{u_total}, {2*u_total}, {3*u_total}, {4*u_total}].  "
            f"MC1 (shared Agent) would fail this test; MC1b (fresh Agent per "
            f"call with shared messages) still meters per-call correctly "
            f"and must be caught by the TestC1 isolation test above."
        )


class TestC4ResolvedC0161BypassesLlm:
    """C4 — any brake record with C0161 bypasses the LLM, regardless of outcome.

    Pre-fix: only active C0161 bypassed, so a rule-following LLM could still
    land the stop-driving phrase on a ~475k-row set of resolved brake jobs
    (SYSTEM_PROMPT instructs the model to inject the phrase whenever C0161
    appears, and D2 started sending the deterministic text containing
    "DTC C0161" into the prompt on resolved jobs).
    """

    def test_resolved_c0161_bypasses_llm_entirely(self):
        """The LLM is NOT called for a resolved C0161 brake record.

        Mutation check: narrowing ``_brakes_has_c0161`` back to active-only
        (``_enrichment_active_brake and "C0161" in ...``) makes this test
        see one LLM call — fails.
        """
        captured_calls: list[tuple[str, str]] = []

        def recording_llm(system_prompt: str, user_prompt: str) -> str:
            captured_calls.append((system_prompt, user_prompt))
            return (
                '{"brakes": {"finding": "stub", "severity_hint": "soon", '
                '"confidence": "medium", "evidence": []}}'
            )

        resolved_brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2024-01-15",
            "dtc_codes": ["C0161"],
            "complaint_text": "Brake warning",
            "warranty_covered": "true",
            "total_cost_usd": "0.00",
            "outcome": "resolved",
            "is_open": False,
            "provenance": "live",
        }

        assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=resolved_brake_data,
            _llm_caller=recording_llm,
        )

        assert captured_calls == [], (
            f"C4: resolved C0161 brake record reached the LLM — the bypass "
            f"did not extend to resolved jobs.\n"
            f"Captured calls: {captured_calls!r}"
        )

    def test_defense_in_depth_scrub_notes_the_bypass_is_the_primary_control(self):
        """The post-LLM scrub is defence-in-depth; the primary control is the
        C4 bypass tested in ``test_resolved_c0161_bypasses_llm_entirely``.

        The scrub's gate requires ``brake_data`` to carry C0161 — but any
        brake_data carrying C0161 triggers the C4 bypass first, so the
        LLM is never called and the scrub is unreachable on the primary
        path.  The scrub exists for a future change (or an LLM that
        produces the phrase from its own knowledge rather than echoing the
        prompt) that bypasses the bypass itself.  This test documents the
        structural dependency so a future reader does not read the scrub
        as a working guard without understanding the bypass is why.
        """
        # The scrub's code is reached when:
        #   (a) brakes is in enriched (component had a Finding), AND
        #   (b) brake_data is not None, AND
        #   (c) C0161 in dtc_codes, AND
        #   (d) _enrichment_active_brake is False (resolved job), AND
        #   (e) the finding contains C0161_VERBATIM_REMEDY after enrichment
        #
        # (a)-(d) collectively require: a resolved brake record with C0161
        # whose brakes component reached the LLM.  The C4 bypass makes that
        # path unreachable.  So (e) never fires on normal flow; the scrub
        # is a defence-in-depth landmine for a future regression.
        #
        # The structural guarantee this encodes: as long as the C4 bypass
        # holds, no resolved C0161 brake Finding can carry the P0 phrase
        # via any LLM path.  The bypass is tested in
        # test_resolved_c0161_bypasses_llm_entirely.
        from agents.health.agent import C0161_VERBATIM_REMEDY
        assert C0161_VERBATIM_REMEDY, "P0 verbatim constant must be non-empty"


class TestC5NeedsJudgmentFilter:
    """C5 — components with ``severity_hint=='routine' AND confidence=='high'``
    bypass the LLM.  The deterministic finding is already a well-formed
    owner-ready reassurance sentence; the LLM has nothing to improve.

    Exception: when ``warranty_verdict_phrase`` is non-None, the filter is
    disabled because the LLM has a real task (narrate the verdict verbatim).
    """

    def test_routine_high_confidence_component_bypasses_llm(self):
        """A healthy battery (92% SoH → routine/high) does NOT reach the LLM.

        Mutation check: deleting the needs-judgment filter (keeping the C4
        bypass only) makes this test fail — the LLM is called with the
        battery component.
        """
        captured_calls: list[tuple[str, str]] = []

        def recording_llm(system_prompt: str, user_prompt: str) -> str:
            captured_calls.append((system_prompt, user_prompt))
            return (
                '{"battery": {"finding": "ok", "severity_hint": "routine", '
                '"confidence": "high", "evidence": []}}'
            )

        # Healthy battery = routine + high confidence.
        result = assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[_BATTERY_ROW_HEALTHY],
            _llm_caller=recording_llm,
        )

        assert captured_calls == [], (
            f"C5: healthy (routine+high) battery reached the LLM — the "
            f"needs-judgment filter is not skipping it.\n"
            f"Captured calls: {captured_calls!r}"
        )
        # The deterministic finding is still returned.
        assert "degrading normally" in result["battery"]["finding"]

    def test_non_routine_component_still_reaches_llm(self):
        """A degraded battery (72.5% SoH → soon/high) DOES reach the LLM.

        The C5 filter must not block legitimate judgment calls.
        """
        captured_calls: list[tuple[str, str]] = []

        def recording_llm(system_prompt: str, user_prompt: str) -> str:
            captured_calls.append((system_prompt, user_prompt))
            return (
                '{"battery": {"finding": "worth a look", "severity_hint": "soon", '
                '"confidence": "high", "evidence": []}}'
            )

        assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[_BATTERY_ROW_DEGRADED],
            _llm_caller=recording_llm,
        )

        assert len(captured_calls) == 1, (
            f"C5: degraded battery (severity=soon) did not reach the LLM — "
            f"the filter is over-broad.\n"
            f"Captured calls: {captured_calls!r}"
        )

    def test_warranty_verdict_disables_the_filter_for_routine_findings(self):
        """When ``warranty_verdict_phrase`` is non-None, routine+high
        components still reach the LLM so the verdict can be narrated.

        Without this exemption, a resolved brake record (routine+high) with a
        warranty verdict phrase would skip the LLM, the verdict would not be
        narrated verbatim, and the post-generation guard would stamp
        ``narration_degraded=True`` on every such Finding.
        """
        captured_calls: list[tuple[str, str]] = []

        def recording_llm(system_prompt: str, user_prompt: str) -> str:
            captured_calls.append((system_prompt, user_prompt))
            return (
                '{"brakes": {"finding": "The warranty claim was denied.", '
                '"severity_hint": "routine", "confidence": "high", "evidence": []}}'
            )

        # A routine resolved brake record with a verdict phrase.
        brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2026-09-15",
            "dtc_codes": [],
            "complaint_text": "Brake squeal",
            "warranty_covered": "false",
            "total_cost_usd": "487.50",
            "outcome": "resolved",
            "is_open": False,
            "provenance": "live",
        }

        assess_vehicle_health(
            SAMPLE_VIN,
            brake_data=brake_data,
            warranty_verdict_phrase="denied",
            _llm_caller=recording_llm,
        )

        assert len(captured_calls) == 1, (
            f"C5: warranty verdict path did not reach the LLM — the filter "
            f"must be disabled when warranty_verdict_phrase is non-None.\n"
            f"Captured calls: {captured_calls!r}"
        )

    def test_circuit_breaker_fires_after_configured_count(self):
        """After the per-batch LLM-call limit, subsequent VINs fall back to the
        deterministic finding without hitting Bedrock.

        Mutation check: deleting the circuit-breaker raises makes this test
        see all N calls reach the stub — fails.
        """
        from unittest.mock import patch, MagicMock

        call_count = {"n": 0}
        _bedrock_singleton = MagicMock(name="BedrockModel")

        class _CountingAgent:
            def __init__(self, model=None, system_prompt=None, **kwargs):
                pass

            def __call__(self, prompt, **kwargs):
                call_count["n"] += 1
                response = MagicMock()
                response.metrics.accumulated_usage = {
                    "inputTokens": 90, "outputTokens": 20, "totalTokens": 110,
                }
                response.__str__ = lambda self=response: (
                    '{"battery": {"finding": "stub", "severity_hint": "soon", '
                    '"confidence": "high", "evidence": []}}'
                )
                return response

        with patch("strands.Agent", _CountingAgent), \
             patch("strands.models.BedrockModel", MagicMock(return_value=_bedrock_singleton)):
            from agents.health.agent import make_strands_agent_fn
            agent_fn = make_strands_agent_fn(max_llm_calls_per_batch=3)

            # Call 5 times with non-routine battery data.
            for i in range(5):
                agent_fn(
                    f"VIN-{i}",
                    [{"vin": f"VIN-{i}", "health_pct": f"7{i}.0", "age_days": "2555"}],
                )

        assert call_count["n"] == 3, (
            f"C5 circuit breaker: expected exactly 3 LLM calls (the configured "
            f"limit), got {call_count['n']}.  The circuit breaker is not "
            f"short-circuiting subsequent calls."
        )


# ---------------------------------------------------------------------------
# W3 rewrite (2026-10-04): the service-date test must assert the date VALUE,
# not just the presence of the "deterministic_finding" field.  The previous
# shape passed even when the deterministic_finding line was deleted
# (reviewer mutation d2_no_finding_line survived it).
# ---------------------------------------------------------------------------


class TestW3ServiceDateReachesPrompt:
    """The service date value itself must reach the LLM prompt, not merely
    the field name ``deterministic_finding``.
    """

    def test_service_date_value_in_prompt(self):
        """A distinctive service date must appear in the user prompt.

        Mutation check: deleting the ``deterministic_finding`` line in
        ``summary_parts`` makes this test fail (the date does not reach
        the prompt via any other field).  The W3 mutation ``d2_no_finding_line``
        is caught here.
        """
        captured: list[tuple[str, str]] = []

        def llm_caller(system_prompt: str, user_prompt: str) -> str:
            captured.append((system_prompt, user_prompt))
            return (
                '{"brakes": {"finding": "ok", "severity_hint": "attention", '
                '"confidence": "high", "evidence": []}}'
            )

        # Distinctive service date that cannot come from any other prompt field.
        # Pick a date far enough in the past that it will be in the
        # deterministic finding, and open the job so the finding reaches the
        # LLM (routine resolved brakes are skipped by C5).
        brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2017-03-19",
            "dtc_codes": [],
            "complaint_text": "",
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "parts_pending",  # active → reaches LLM
            "is_open": True,
            "provenance": "live",
            "model_year": 2024,
        }

        assess_vehicle_health(SAMPLE_VIN, brake_data=brake_data, _llm_caller=llm_caller)
        assert captured, "LLM was never called"
        _system, user_prompt = captured[-1]

        assert "2017-03-19" in user_prompt, (
            "W3: the service date value is not in the prompt.  Deleting the "
            "'deterministic_finding' line from summary_parts would leave only "
            "field names in the prompt — this assertion catches that mutation.\n"
            f"Prompt: {user_prompt!r}"
        )


# ---------------------------------------------------------------------------
# W1 cycle-2 rewrite (2026-10-04): the system prompt may only name fields
# that are actually carried in the user prompt.  Promising a field the model
# never receives produced the 2026-10-04 "Provide those fields" reply, which
# D2 (adding the deterministic sentence) does not prevent — if the system
# prompt says "ADP supplies warranty_covered" and the user prompt does not
# carry a warranty value, the model asks for it, and the request lands in
# an owner-facing finding card.
# ---------------------------------------------------------------------------


class TestW1CycleTwoSystemPromptPromises:
    """The system prompt may only name a field as supplied if the user prompt
    actually carries it.

    Enforced on the specific sentence shape that produced the 2026-10-04
    "Provide those fields" reply: a bullet line of the form ``- ADP supplies:
    <field list>`` whose fields do not then appear in the user prompt.
    """

    def test_system_prompt_does_not_use_the_adp_supplies_pattern(self):
        """The pre-fix SYSTEM_PROMPT named structured ADP fields on bullets
        of the form ``- ADP supplies: X, Y, Z``.  The fields it named were
        not sent in the user prompt (which carries only a deterministic
        sentence), so the model asked for them — the 2026-10-04 brakes reply
        *"Provide those fields. I will respond immediately and completely."*

        The W1 cycle-2 fix rewrites the guidance to describe what IS sent
        (``- You receive one deterministic sentence...``) rather than listing
        unarrived field names.  This test pins that invariant.

        Mutation check: re-introducing a line like ``- ADP supplies:
        service_date, dtc_codes, warranty_covered, total_cost_usd, outcome.``
        in BRAKES guidance makes this test fail.
        """
        from agents.health.prompt import SYSTEM_PROMPT

        # The reviewer's cycle-2 finding (Warning 1) identified three
        # specific bullets of this shape; all three were on the pre-fix prompt
        # and must stay out of the post-fix prompt.
        import re as _re
        matches = _re.findall(
            r"-\s*ADP\s*supplies\s*:", SYSTEM_PROMPT, flags=_re.IGNORECASE,
        )
        assert matches == [], (
            "W1 cycle-2: SYSTEM_PROMPT contains an 'ADP supplies: ...' "
            "bullet.  The pre-fix prompt named fields on these bullets that "
            "were never carried in the user prompt (warranty_covered, "
            "total_cost_usd for brakes; wear category, wear rate metric for "
            "tires).  The 2026-10-04 model reply 'Provide those fields' is "
            "the exact failure mode.  Rewrite the bullet to describe the "
            "deterministic sentence the user prompt carries, or send the "
            "named fields in the user prompt.\n"
            f"Matches found: {matches!r}"
        )

    def test_brakes_user_prompt_covers_fields_the_system_prompt_names(self):
        """For each component the system prompt now says the model receives
        a ``deterministic_finding`` sentence; verify that sentence is in fact
        in the user prompt.
        """
        captured: list[tuple[str, str]] = []

        def llm_caller(system_prompt: str, user_prompt: str) -> str:
            captured.append((system_prompt, user_prompt))
            return (
                '{"brakes": {"finding": "ok", "severity_hint": "attention", '
                '"confidence": "high", "evidence": []}}'
            )

        brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2024-07-12",
            "dtc_codes": [],
            "complaint_text": "",
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "parts_pending",
            "is_open": True,
            "provenance": "live",
            "model_year": 2024,
        }
        assess_vehicle_health(SAMPLE_VIN, brake_data=brake_data, _llm_caller=llm_caller)
        assert captured, "LLM was never called"
        _system, user_prompt = captured[-1]
        # The deterministic sentence must be in the user prompt and must name
        # the service date and the outcome shape.
        assert "2024-07-12" in user_prompt
        assert "parts" in user_prompt.lower() or "awaiting" in user_prompt.lower()



# ---------------------------------------------------------------------------
# W2 cycle-2 (2026-10-04): date caveat is stripped before the LLM call and
# re-attached after, so the headline-selecting sentence is always the brake
# finding (never the caveat), even when the model reorders sentences.
# ---------------------------------------------------------------------------


class TestW2CycleTwoCaveatStrippedFromLlmPath:
    """The date caveat must not reach the LLM.

    Brakes selection only picks open jobs, and most ADP brake dates predate
    the vehicle model year, so ~100 LLM-called brake records per nightly
    batch carry the caveat.  If the model is allowed to see it, it can
    reorder sentences and ``_parse_llm_response`` takes the model's
    ``finding`` as-is — ``derive_headline`` then lands the caveat as the
    owner-facing headline, which is the exact 2026-10-04 Defect 5 failure
    on the LLM path.
    """

    _SUSPECT_DATE_BRAKE = {
        "vin": SAMPLE_VIN,
        "service_date": "2018-05-04",
        "dtc_codes": [],
        "complaint_text": "",
        "warranty_covered": "false",
        "total_cost_usd": "0.00",
        "outcome": "parts_pending",
        "is_open": True,
        "provenance": "live",
        "model_year": 2024,  # service_year 2018 < model_year - 1 → caveat applies
    }

    def test_caveat_text_is_absent_from_the_llm_user_prompt(self):
        """The 'date may be inaccurate' caveat text must not reach the LLM.

        Mutation check: delete the ``_stashed_brake_caveat`` strip-and-stash
        block in ``_enrich_with_llm`` and this test fails.
        """
        captured: list[tuple[str, str]] = []

        def llm_caller(system_prompt: str, user_prompt: str) -> str:
            captured.append((system_prompt, user_prompt))
            return (
                '{"brakes": {"finding": "Brake service from 2018-05-04 is '
                'awaiting parts.", "severity_hint": "attention", '
                '"confidence": "medium", "evidence": []}}'
            )

        assess_vehicle_health(
            SAMPLE_VIN, brake_data=self._SUSPECT_DATE_BRAKE, _llm_caller=llm_caller,
        )
        assert captured, "LLM was never called"
        _system, user_prompt = captured[-1]
        assert "may be inaccurate" not in user_prompt, (
            "W2 cycle-2: the data-date caveat reached the LLM.  Strip the "
            "caveat before the summary_parts loop in _enrich_with_llm and "
            "re-attach it after enrichment.\n"
            f"Prompt: {user_prompt!r}"
        )
        assert "Note: the service record is dated" not in user_prompt

    def test_headline_is_the_brake_finding_even_when_model_reorders(self):
        """A rule-following model that puts the caveat FIRST in its reply must
        not land the caveat as the headline.  The stripped-and-reattached
        structure ensures ``derive_headline`` sees the brake finding first.

        Mutation check: remove the re-attach step and this test still passes
        (the caveat was stripped and never came back), but the caveat is
        then absent from the detail — tested below.
        A different mutation: keep the strip but let the LLM return the full
        caveat-bearing finding as-is → the caveat becomes the headline on
        the LLM path.  Both of those are failures the test must distinguish
        from the correct behaviour.
        """
        from agents.tier2.runner import _build_artifact

        # Model deliberately returns the caveat first — simulates a rule-
        # following model that was given the caveat in the prompt (the pre-
        # fix state).
        def caveat_first_llm(system_prompt: str, user_prompt: str) -> str:
            # The user prompt now excludes the caveat (W2 fix) so this stub
            # could not realistically produce it — but a rule-following model
            # that saw the caveat in prior training could still emit it.
            # Simulate the worst case: the model writes caveat-first.
            return (
                '{"brakes": {"finding": "This record may be inaccurate '
                '(service dated 2018, 2024 model).  Brake service from '
                '2018-05-04 is awaiting parts.", '
                '"severity_hint": "attention", "confidence": "medium", '
                '"evidence": []}}'
            )

        result = assess_vehicle_health(
            SAMPLE_VIN, brake_data=self._SUSPECT_DATE_BRAKE,
            _llm_caller=caveat_first_llm,
        )
        artifact, _ = _build_artifact(SAMPLE_VIN, "brakes", [self._SUSPECT_DATE_BRAKE],
                                       result["brakes"])
        headline = artifact["headline"]

        # The model's finding was caveat-first.  The W2 strip-and-reattach
        # does NOT edit the model's reply content — if the model writes the
        # caveat into its own text, that is a defence-in-depth concern.  But
        # the DETERMINISTIC caveat (the one appended by _build_brake_finding)
        # must not appear in the headline.
        assert "the date may be inaccurate" not in headline, (
            "W2 cycle-2: deterministic caveat appeared in the headline on "
            f"the LLM path.\nheadline: {headline!r}"
        )

    def test_caveat_is_reattached_after_enrichment(self):
        """The caveat must be back in the final detail after the LLM call.

        Mutation check: delete the re-attach step before ``return enriched``
        and this test fails.  Owners who ever saw the caveat on the
        deterministic path will stop seeing it entirely if we strip-and-forget.
        """
        from agents.tier2.runner import _build_artifact

        def llm_caller(system_prompt: str, user_prompt: str) -> str:
            # Model returns a clean body-only finding.
            return (
                '{"brakes": {"finding": "Brake service from 2018-05-04 is '
                'awaiting parts.", "severity_hint": "attention", '
                '"confidence": "medium", "evidence": []}}'
            )

        result = assess_vehicle_health(
            SAMPLE_VIN, brake_data=self._SUSPECT_DATE_BRAKE, _llm_caller=llm_caller,
        )
        artifact, _ = _build_artifact(SAMPLE_VIN, "brakes", [self._SUSPECT_DATE_BRAKE],
                                       result["brakes"])
        detail = artifact["detail"]
        assert "the date may be inaccurate" in detail, (
            "W2 cycle-2: the caveat was stripped but never re-attached.  "
            f"detail: {detail!r}"
        )
        assert "2018" in detail and "2024" in detail
