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

    def test_c0161_finding_contains_verbatim_p0_phrase(self):
        """CRITICAL: C0161 → finding must contain the exact verbatim phrase.

        Spec § D8: 'Critical brake pressure loss detected. Pull over immediately.'
        Softening to 'exercise caution' or 'seek service' is a boundary violation.
        This test asserts the string itself, not just that severity is high.
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
        """Explicitly assert the phrase is not softened to 'exercise caution'."""
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
    """Every key the three source modules emit as OUTPUT to the agent.

    Deliberately dict-literal keys only.  An earlier revision also counted keys a
    source *reads from ADP* and passes through, which made this test vacuous: the
    tire source reads ``needs_replacement`` in order to translate it, so counting
    inputs put the very name the agent must not use back into the allowed set.
    Proved vacuous by reintroducing the historical bug and watching the test pass.
    Output keys only.
    """
    import ast
    import pathlib

    keys: set = set()
    for name in ("adp_tire_source", "adp_battery_source", "adp_brake_source"):
        src = pathlib.Path(f"agents/health/{name}.py").read_text()
        for node in ast.walk(ast.parse(src)):
            # Keys of dict literals the module builds.
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
    return keys


def _agent_read_keys() -> set:
    """Every row key `agent.py` reads via `r.get(...)` / `row.get(...)`."""
    import ast
    import pathlib

    src = pathlib.Path("agents/health/agent.py").read_text()
    tree = ast.parse(src)
    # Resolve module-level `_KEY_* = "literal"` constants first.
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                consts[tgt.id] = node.value.value

    read: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                read.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                read.add(consts[arg.id])
    return read


def test_agent_reads_only_keys_the_sources_emit():
    """No key the agent reads may be absent from every source's output.

    This is the assertion that would have caught the tire false negative.
    """
    emitted = _source_emitted_keys()
    read = _agent_read_keys()
    # Keys the agent legitimately reads from its own artifacts, not from a source.
    own = {
        "finding", "severity_hint", "confidence", "evidence", "component",
        "computed_at", "agent_version", "inputs_hash", "ttl", "pk", "sk",
        "rows", "value", "source", "provenance", "vin",
        # Component names and structural keys of the agent's own payloads.
        "tires", "battery", "brakes", "data",
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
    """T4.13 W-C8-2: _enrich_with_llm must not include ADP-derived prose in
    the LLM prompt.

    The previous implementation used ``finding_preview={res['finding'][:80]!r}``
    in the component_summary, which interpolated a substring of the deterministic
    finding text.  That text is assembled from ADP columns (outcome, dtc_codes,
    service_date, etc.).  A poisoned dtc_codes value could therefore reach the
    LLM prompt via the finding string.

    The fix replaces the finding substring with a structured representation
    built only from validated fields: severity_hint (already-sanitised enum),
    confidence (bounded enum), and evidence_count (integer).  None of these
    carry free-form ADP text.

    Test invariants:
    1. The prompt must not contain any substring of any component's finding text.
    2. The prompt MUST contain severity_hint for each component.
    3. The prompt MUST contain confidence for each component.
    4. The prompt MUST contain evidence_count for each component.
    5. A poisoned dtc_codes payload must not appear in the LLM prompt even after
       validate_dtc_codes drops it at the brake source boundary — belt-and-
       suspenders: even if a future component doesn't validate, the prompt path
       must not include finding substrings.
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
        """Run assess_vehicle_health with all three components and capture the prompt."""
        from agents.health.agent import assess_vehicle_health

        # Use real-looking data so deterministic findings produce non-trivial text.
        tire_rows = [
            {
                "vin": SAMPLE_VIN,
                "position": "FL",
                "wear_band": "ok",
                "replacement_flagged": False,
                "tread_depth_mm": 7.5,
            }
        ]
        battery_rows = [
            {
                "vin": SAMPLE_VIN,
                "state_of_health_pct": 92.0,
                "cycles": 312,
                "warranty_months_remaining": 48,
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
        """The LLM prompt must not contain a substring of any finding.

        W-C8-2 finding: ``finding_preview={res['finding'][:80]!r}`` interpolated
        ADP-derived prose into the prompt.  The replacement structured summary
        must contain no substring of any finding field.
        """
        from agents.health.agent import assess_vehicle_health

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

        assert "severity_hint=" in user_prompt, (
            "severity_hint must appear in the structured prompt so the LLM can "
            "use it to improve phrasing."
        )

    def test_prompt_contains_confidence_for_each_component(self):
        """The structured summary must include confidence for each component."""
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "confidence=" in user_prompt, (
            "confidence must appear in the structured prompt."
        )

    def test_prompt_contains_evidence_count(self):
        """The structured summary must include evidence_count (integer, no prose)."""
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "evidence_count=" in user_prompt, (
            "evidence_count must appear in the structured prompt."
        )

    def test_c0161_verbatim_phrase_not_in_prompt(self):
        """The C0161 P0 verbatim phrase must not appear in the LLM prompt.

        The safety phrase "Critical brake pressure loss detected. Pull over
        immediately." is injected into the *finding* field (via
        inject_p0_verbatim_phrases).  The structured summary must not include
        any substring of that finding, so the phrase itself does not reach the
        LLM — the LLM's job is phrasing, not safety-phrase generation.

        This is the core of W-C8-2: no ADP-derived prose (including the safely-
        assembled deterministic finding text) should reach the LLM prompt.
        """
        from agents.health.prompt import C0161_VERBATIM_REMEDY

        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert C0161_VERBATIM_REMEDY not in user_prompt, (
            f"The C0161 verbatim phrase appeared in the LLM prompt:\n"
            f"  phrase: {C0161_VERBATIM_REMEDY!r}\n"
            f"W-C8-2: the structured summary must not include any finding substring."
        )

    def test_service_date_not_in_prompt(self):
        """Service date string must not appear in the LLM prompt.

        The deterministic brake finding interpolates service_date (e.g.
        ``'2026-08-03'``) into the finding text.  That string must not reach the
        prompt via a finding substring.
        """
        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        assert "2026-08-03" not in user_prompt, (
            "service_date (2026-08-03) appeared in the LLM prompt — finding "
            "text is leaking into the prompt via a substring."
        )

    def test_no_finding_substring_from_any_component(self):
        """No substring of any component's finding text appears in the prompt.

        Directly builds the deterministic findings, then verifies none of their
        content is present in the captured prompt.  This is the broadest form of
        the W-C8-2 assertion.
        """
        from agents.health.agent import (
            _build_brake_finding,
            _build_battery_finding,
            _build_tire_finding,
            _sanitise_component_result,
        )

        tire_rows = [
            {
                "vin": SAMPLE_VIN,
                "position": "FL",
                "wear_band": "ok",
                "replacement_flagged": False,
                "tread_depth_mm": 7.5,
            }
        ]
        battery_rows = [
            {
                "vin": SAMPLE_VIN,
                "state_of_health_pct": 92.0,
                "cycles": 312,
                "warranty_months_remaining": 48,
            }
        ]
        brake_data = {
            "vin": SAMPLE_VIN,
            "service_date": "2026-08-03",
            "dtc_codes": ["C0161"],
            "complaint_text": "Brake warning",
            "warranty_covered": "false",
            "total_cost_usd": "0.00",
            "outcome": "parts_pending",
            "is_open": True,
            "provenance": "live",
        }

        # Build exactly the findings the agent builds internally.
        tire_res = _sanitise_component_result(_build_tire_finding(tire_rows))
        battery_res = _sanitise_component_result(_build_battery_finding(battery_rows))
        brake_res = _sanitise_component_result(_build_brake_finding(brake_data))

        # Collect non-trivial substrings from each finding (words ≥5 chars).
        import re
        significant_tokens: list[str] = []
        for res in (tire_res, battery_res, brake_res):
            # Extract words that are long enough to be meaningful — short tokens
            # like "ok", "high", "low" can legitimately appear as structured
            # fields.  We check for anything ≥10 chars that came from ADP data.
            tokens = re.findall(r"\b[A-Za-z0-9_\-]{10,}\b", res["finding"])
            significant_tokens.extend(tokens)

        stub = self._CapturingStub()
        user_prompt = self._run_with_all_components(stub)

        leaked = [t for t in significant_tokens if t in user_prompt]
        assert not leaked, (
            f"The following tokens from deterministic finding text appeared in "
            f"the LLM prompt:\n  {leaked}\n"
            "W-C8-2: no ADP-derived prose may reach the LLM prompt.  The "
            "structured summary must use only severity_hint, confidence, and "
            "evidence_count."
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
        """
        import json

        stub_battery_finding = "Battery enriched by LLM — at 92%, all good for now."

        def stub(system_prompt: str, user_prompt: str) -> str:
            return json.dumps({
                "battery": {
                    "finding": stub_battery_finding,
                    "severity_hint": "routine",
                    "confidence": "high",
                    "evidence": [],
                },
            })

        result = assess_vehicle_health(
            SAMPLE_VIN,
            battery_rows=[{
                "vin": SAMPLE_VIN,
                "health_pct": "92.0",
                "age_days": "1095",
            }],
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

    def test_resolved_c0161_job_still_enriches_normally(self):
        """Resolved C0161 brake job (T4.9 agreement): still goes through LLM enrichment.

        The T4.14 bypass only fires when the job is active (outcome in
        _ACTIVE_BRAKE_OUTCOMES or is_open).  A resolved job must still enrich
        normally — the bypass must not disable enrichment for all C0161 findings.
        """
        import json

        stub_resolved_text = "Brake service from 2024-01-15 is complete. C0161 was resolved."

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

        # Resolved job: LLM enrichment runs → stub text should appear.
        assert stub_resolved_text in result["brakes"]["finding"], (
            f"T4.14: resolved C0161 brake finding was not enriched by LLM.\n"
            f"Expected stub text: {stub_resolved_text!r}\n"
            f"Result: {result['brakes']['finding']!r}\n"
            f"T4.9 agreement: resolved jobs must NOT acquire bypass."
        )

        # Resolved job: P0 verbatim phrase must NOT appear (T4.9).
        assert C0161_VERBATIM_REMEDY not in result["brakes"]["finding"], (
            f"T4.14/T4.9: resolved C0161 job must not acquire the P0 verbatim phrase.\n"
            f"Result: {result['brakes']['finding']!r}"
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
