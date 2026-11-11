"""Red tests — FI fleet-health agent (task 1.4 items 1–6).

Covers:

  1. Band table (spec D9, contract § 1.3): each row gives the expected severity and items;
     a grounding item exists exactly when an open p0 fault exists and the vehicle is not
     already grounded; a fake model reply asking to add, drop or change a grounding, a
     need or a severity changes nothing.
  2. disclosure_txt byte-equal to the fault catalog's directive on both p0 items (booking
     and grounding); headlines only from the fixed templates (contract § 1.4–1.5).
  3. Service-plan validator (spec D8 step 7): rooftop outside the candidates, a deferred
     p0/attention need, an urgency the band forbids — each rejected; one retry, then the
     rules fallback.
  4. Skip rule and cap (60), mirroring Stage C D8 step 4; Athena selection pages through
     NextToken to the end (stub returning two pages).
  5. Attempt rules for both subject forms (spec D9, contract § 6).
  6. fault_grounding_enabled false → no grounding item, booking item is unaffected.

Tests were xfail(strict=True) until tasks 3.x/4.1 built the code. All not-yet-built module
imports are inside test bodies so collection never errors. Use CSV custom:fleetIds in
fixtures.

Run:
    .venv/bin/python -m pytest agents/fi_fleet_health/tests -q
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VIN = "MRDVEH00000000017"
_FLEET_ID = "fleet-meridian-01"

#: CSV custom:fleetIds per task constraint.
_OPERATOR_CLAIMS = {
    "sub": "op-sub-fh-0001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",
}

#: A minimal fault catalog entry for a p0 (stop-driving) code.
_P0_CODE = "P0AA6"
_P0_CATALOG_ENTRY = {
    "code": _P0_CODE,
    "description": "High-voltage isolation fault.",
    "system": "ev_drivetrain",
    "band": "p0",
    "applies_to": ["ev", "hybrid"],
    "directive": (
        "Stop driving immediately. "
        "Do not restart the vehicle. "
        "Contact your dealer for towing to the nearest service centre."
    ),
}

#: A minimal attention-band fault entry.
_ATTENTION_CODE = "P0A7F"
_ATTENTION_CATALOG_ENTRY = {
    "code": _ATTENTION_CODE,
    "description": "Battery system pack performance degraded.",
    "system": "ev_battery",
    "band": "attention",
    "applies_to": ["ev"],
    "directive": None,
}

#: A soon-band fault entry.
_SOON_CODE = "P1AB0"
_SOON_CATALOG_ENTRY = {
    "code": _SOON_CODE,
    "description": "Charging system fault.",
    "system": "ev_charging",
    "band": "soon",
    "applies_to": ["ev"],
    "directive": None,
}


#: Fixed detail text for the rules fallback path (D8 step 7).
#: The implementation MUST produce exactly this string; model-generated text must not
#: reach this field on the rules path.
_RULES_FALLBACK_DETAIL = (
    "Service plan from deterministic rules: nearest candidate selected, "
    "no needs deferred (model output failed validation after retry)."
)


# ============================================================================
# Item 1 — Band table
# ============================================================================

class TestBandTable:
    """Spec D9 / contract § 1.3: deterministic severity and items from the needs list."""

    def test_p0_open_fault_gives_p0_severity(self):
        """A single open p0 fault → severity p0."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
        )
        assert result.severity == "p0"

    def test_p0_fault_produces_booking_and_grounding_items(self):
        """p0 open fault + not grounded + flag on → booking AND grounding items."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
        )
        assert "booking" in result.items
        assert "grounding" in result.items

    def test_attention_fault_gives_attention_severity_booking_only(self):
        """Open attention fault → attention severity, booking item only."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _ATTENTION_CODE, "band": "attention"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
        )
        assert result.severity == "attention"
        assert result.items == ["booking"]
        assert "grounding" not in result.items

    def test_attention_agent2_finding_gives_attention_severity(self):
        """Agent 2 finding at attention → attention severity, booking only."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[],
            agent2_findings=[{"severity": "attention", "finding_id": "fi-001"}],
            pm_tasks=[],
        )
        assert result.severity == "attention"
        assert "grounding" not in result.items

    def test_soon_fault_gives_soon_severity_booking_only(self):
        """Open soon-band fault → soon severity, booking only."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _SOON_CODE, "band": "soon"}],
            agent2_findings=[],
            pm_tasks=[],
        )
        assert result.severity == "soon"
        assert result.items == ["booking"]

    def test_overdue_pm_task_gives_soon_severity(self):
        """PM task `overdue` → soon severity, booking only."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[],
            agent2_findings=[],
            pm_tasks=[{"task_code": "TIRE_ROTATION", "status": "overdue"}],
        )
        assert result.severity == "soon"
        assert result.items == ["booking"]

    def test_due_pm_task_only_gives_routine_severity(self):
        """PM task `due` and nothing else → routine severity, booking only."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[],
            agent2_findings=[],
            pm_tasks=[{"task_code": "BRAKE_INSPECTION", "status": "due"}],
        )
        assert result.severity == "routine"
        assert result.items == ["booking"]

    def test_most_severe_need_wins_p0_over_routine(self):
        """When a p0 fault and a due PM task both present, p0 wins."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[{"task_code": "BRAKE_INSPECTION", "status": "due"}],
            already_grounded=False,
            fault_grounding_enabled=True,
        )
        assert result.severity == "p0"
        assert "grounding" in result.items

    def test_no_grounding_when_already_grounded(self):
        """p0 fault but vehicle already grounded → booking only, no second grounding."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=True,
            fault_grounding_enabled=True,
        )
        assert result.severity == "p0"
        assert "grounding" not in result.items
        assert "booking" in result.items

    def test_model_hint_cannot_remove_grounding_from_p0(self):
        """Mutation guard: a model reply asking to skip grounding on a p0 must be ignored.

        The grounding item is decided by the band table, never by model output (spec D9).
        Mutation: bands.classify_needs uses model_hint to decide grounding → fails.
        """
        from agents.fi_fleet_health import bands

        fake_model_hint = {"skip_grounding": True, "severity_override": "routine"}
        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
            model_hint=fake_model_hint,
        )
        assert result.severity == "p0"
        assert "grounding" in result.items, (
            "Model hint asking to skip grounding must be ignored (spec D9: grounding is "
            "decided by the band table, never by model output)."
        )

    def test_model_hint_cannot_add_grounding_to_non_p0(self):
        """Model reply asking to add grounding on a non-p0 need is ignored."""
        from agents.fi_fleet_health import bands

        fake_model_hint = {"add_grounding": True}
        result = bands.classify_needs(
            open_faults=[{"code": _ATTENTION_CODE, "band": "attention"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
            model_hint=fake_model_hint,
        )
        assert result.severity == "attention"
        assert "grounding" not in result.items

    def test_model_hint_cannot_change_severity(self):
        """Model hint proposing a different severity is ignored."""
        from agents.fi_fleet_health import bands

        fake_model_hint = {"severity": "soon"}  # tries to downgrade p0 to soon
        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
            model_hint=fake_model_hint,
        )
        assert result.severity == "p0"

    def test_model_hint_cannot_drop_or_add_needs(self):
        """Model reply dropping or adding a need changes nothing in the classify result.

        Fix C1: also asserts the needs list so a writer that accepts model_hint is caught.
        Mutation: let classify_needs accept model_hint['add_needs'] or ['drop_needs'] →
        fabricated_need appears in result.needs or TIRE_ROTATION disappears, this fails.
        """
        from agents.fi_fleet_health import bands

        fake_model_hint = {
            "drop_needs": ["tire_rotation"],
            "add_needs": ["fabricated_need"],
        }
        result = bands.classify_needs(
            open_faults=[],
            agent2_findings=[],
            pm_tasks=[{"task_code": "TIRE_ROTATION", "status": "due"}],
            already_grounded=False,
            fault_grounding_enabled=True,
            model_hint=fake_model_hint,
        )
        # TIRE_ROTATION is due → routine severity; the drop request has no effect.
        assert result.severity == "routine"
        # Fix C1: assert the needs list is not altered by the model hint.
        need_codes = [
            n if isinstance(n, str) else n.get("task_code", str(n))
            for n in result.needs
        ]
        assert "fabricated_need" not in need_codes, (
            "Model hint adding 'fabricated_need' must be ignored (spec D9: needs come from "
            "open_faults, agent2_findings and pm_tasks only). "
            "Mutation: let classify_needs add model_hint needs → fabricated_need appears, fails."
        )
        assert any("TIRE_ROTATION" in str(n).upper() for n in result.needs), (
            "Model hint dropping TIRE_ROTATION must be ignored — the need must still be present. "
            "Mutation: let classify_needs drop model_hint['drop_needs'] → absent, fails."
        )


# ============================================================================
# Item 2 — disclosure_txt and fixed headline templates
# ============================================================================

class TestDisclosureAndHeadlines:
    """disclosure_txt is the fault catalog's directive byte for byte (contract § 1.5).
    Headlines come from the fixed templates only (contract § 1.4).
    """

    def test_p0_booking_disclosure_txt_equals_catalog_directive(self):
        """disclosure_txt on the booking item is the catalog directive, byte for byte."""
        from agents.fi_fleet_health import writer

        txt = writer.build_disclosure_txt(fault_catalog_entry=_P0_CATALOG_ENTRY)
        assert txt == _P0_CATALOG_ENTRY["directive"], (
            "disclosure_txt must be the catalog directive byte for byte (contract § 1.5). "
            "Mutation: return a paraphrase → byte-equality fails."
        )

    def test_p0_grounding_disclosure_txt_equals_catalog_directive(self):
        """Grounding item carries the catalog directive byte for byte (contract § 1.5).

        Fix C1: builds the item via write.build_grounding_item (not just the helper)
        so that a writer that ignores the catalog and copies disclosure_txt from the
        model reply is caught.
        Mutation: return a model-supplied paraphrase from build_grounding_item →
        byte-equality with the catalog directive fails.
        """
        from agents.fi_fleet_health import writer

        item = writer.build_grounding_item(
            vin=_VIN,
            attempt=1,
            fault_codes=[_P0_CODE],
            last_sighting=[{"code": _P0_CODE, "last_seen_at": "2026-10-01T00:00:00Z"}],
            catalog_entry=_P0_CATALOG_ENTRY,
            model_fn=lambda *a, **k: {
                "disclosure_txt": "WRONG model paraphrase",
                "headline": "model-supplied wrong headline",
            },
        )
        assert item["disclosure_txt"] == _P0_CATALOG_ENTRY["directive"], (
            "disclosure_txt on the grounding item must be the catalog directive byte for "
            "byte, not a model-supplied paraphrase (contract § 1.5). "
            "Mutation: copy disclosure_txt from model reply → byte-equality fails."
        )
        # C6: grounding_reason must carry last_sighting with last_seen_at (contract § 1.6).
        grounding_reason = item.get("grounding_reason", {})
        sightings = grounding_reason.get("last_sighting", [])
        assert any(s.get("code") == _P0_CODE and s.get("last_seen_at") for s in sightings), (
            "grounding_reason.last_sighting must carry an entry for each p0 code with "
            "last_seen_at set (contract § 1.6). "
            "Mutation: use first_sightings / first_seen → no last_seen_at, this fails."
        )

    def test_disclosure_txt_mutation_trim_fails(self):
        """Mutation guard: trimmed text does not equal the full directive."""
        from agents.fi_fleet_health import writer

        txt = writer.build_disclosure_txt(fault_catalog_entry=_P0_CATALOG_ENTRY)
        trimmed = _P0_CATALOG_ENTRY["directive"][:10]
        assert txt != trimmed, (
            "build_disclosure_txt must not truncate the directive. "
            "A 10-char trim must not equal the full text."
        )

    def test_non_p0_item_has_no_disclosure_txt(self):
        """Items below p0 must not carry disclosure_txt (contract § 1.5)."""
        from agents.fi_fleet_health import writer

        txt = writer.build_disclosure_txt(fault_catalog_entry=_ATTENTION_CATALOG_ENTRY)
        assert txt is None

    def test_headline_booking_p0(self):
        """p0 booking headline: 'Stop-driving fault <code>: book service at <rooftop>'."""
        from agents.fi_fleet_health import writer

        headline = writer.build_headline(
            item_type="booking",
            severity="p0",
            fault_code=_P0_CODE,
            need_description=None,
            rooftop_dealer_id="dealer-meridian-001",
        )
        assert headline == f"Stop-driving fault {_P0_CODE}: book service at dealer-meridian-001"

    def test_headline_booking_attention(self):
        """attention booking headline: 'Attention needed: book service at <rooftop>'."""
        from agents.fi_fleet_health import writer

        headline = writer.build_headline(
            item_type="booking",
            severity="attention",
            fault_code=None,
            need_description=None,
            rooftop_dealer_id="dealer-meridian-001",
        )
        assert headline == "Attention needed: book service at dealer-meridian-001"

    def test_headline_booking_soon(self):
        """soon booking headline: 'Service needed soon: book service at <rooftop>'."""
        from agents.fi_fleet_health import writer

        headline = writer.build_headline(
            item_type="booking",
            severity="soon",
            fault_code=None,
            need_description=None,
            rooftop_dealer_id="dealer-meridian-002",
        )
        assert headline == "Service needed soon: book service at dealer-meridian-002"

    def test_headline_booking_routine(self):
        """routine booking headline: 'PM due: book service at <rooftop>'."""
        from agents.fi_fleet_health import writer

        headline = writer.build_headline(
            item_type="booking",
            severity="routine",
            fault_code=None,
            need_description=None,
            rooftop_dealer_id="dealer-meridian-003",
        )
        assert headline == "PM due: book service at dealer-meridian-003"

    def test_headline_grounding_fixed(self):
        """Grounding headline: 'Stop-driving fault <code>: ground until the repair is recorded'."""
        from agents.fi_fleet_health import writer

        headline = writer.build_headline(
            item_type="grounding",
            severity="p0",
            fault_code=_P0_CODE,
            need_description=None,
            rooftop_dealer_id=None,
        )
        assert headline == (
            f"Stop-driving fault {_P0_CODE}: ground until the repair is recorded"
        )

    def test_unknown_item_type_raises(self):
        """Unknown item_type must raise rather than produce a headline."""
        from agents.fi_fleet_health import writer

        with pytest.raises((ValueError, KeyError)):
            writer.build_headline(
                item_type="fabricated_type",
                severity="p0",
                fault_code=_P0_CODE,
                need_description=None,
                rooftop_dealer_id=None,
            )

    def test_grounding_item_gets_no_model_call(self):
        """build_grounding_item must not invoke a model callable (fixed text + no model).

        Fix C1: also asserts disclosure_txt, headline and fault_codes so that a writer
        that calls the model or omits the directive is caught by this test alone.
        """
        from agents.fi_fleet_health import writer

        model_called: list = []

        item = writer.build_grounding_item(
            vin=_VIN,
            attempt=1,
            fault_codes=[_P0_CODE],
            last_sighting=[{"code": _P0_CODE, "last_seen_at": "2026-10-01T00:00:00Z"}],
            catalog_entry=_P0_CATALOG_ENTRY,
            model_fn=lambda *a, **k: model_called.append(True),
        )
        assert len(model_called) == 0, (
            "Grounding items get fixed text and no model call (spec D8 step 6)."
        )
        assert "recommended_action" in item
        assert item["recommended_action"]["action_kind"] == "ground_vehicle"
        # Fix C1: assert disclosure_txt is the directive (not model-sourced or absent).
        assert item.get("disclosure_txt") == _P0_CATALOG_ENTRY["directive"], (
            "Grounding item must carry disclosure_txt == catalog directive (contract § 1.5). "
            "Mutation: omit disclosure_txt or copy from model reply → this fails."
        )
        # Fix C1: assert the grounding headline is the fixed template.
        expected_headline = (
            f"Stop-driving fault {_P0_CODE}: ground until the repair is recorded"
        )
        assert item.get("headline") == expected_headline, (
            f"Grounding item headline must be the fixed template '{expected_headline}'. "
            "Mutation: copy headline from model reply → mismatch, this fails."
        )
        # Fix C1: assert fault_codes is forwarded in the recommended_action parameters.
        fault_codes_sent = item["recommended_action"].get("parameters", {}).get("fault_codes", [])
        assert _P0_CODE in fault_codes_sent, (
            f"Grounding item must carry fault_codes=['{_P0_CODE}'] in recommended_action. "
            "Mutation: omit fault_codes or put recall_id → assertion fails."
        )
        # Fix C6: grounding_reason must carry last_sighting with last_seen_at per code
        # (contract § 1.6). A writer that passes first_sightings / first_seen instead
        # will produce an absent last_sighting or a missing last_seen_at key.
        grounding_reason = item.get("grounding_reason", {})
        last_sighting = grounding_reason.get("last_sighting", [])
        codes_in_sighting = {s.get("code") for s in last_sighting}
        assert _P0_CODE in codes_in_sighting, (
            f"grounding_reason.last_sighting must include an entry for {_P0_CODE!r}. "
            "Mutation: use first_sightings dict → last_sighting absent or wrong key, "
            "this fails."
        )
        for s in last_sighting:
            assert "last_seen_at" in s, (
                "Each last_sighting entry must carry last_seen_at (contract § 1.6). "
                "Mutation: field named first_seen or raised_at → key absent, this fails."
            )


# ============================================================================
# Item 2 extension — Full writer with stub model (Fix C1)
# ============================================================================

class TestWriterWithStubModel:
    """Fix C1: drives build_all_items / the full writer with a stub model.

    Adds the test the review found missing: a stub model that carries severity,
    headline, extra needs and a grounding flag must not alter the written items.
    Both the booking and grounding items must carry the catalog directive byte
    for byte and the fixed headline template.
    """

    def test_stub_model_reply_cannot_alter_booking_or_grounding_items(self):
        """Full writer: model reply with bad severity/headline/needs/grounding_flag
        must not change the written items (spec D9, contract § 1.4–1.5).

        Mutation (severity): let build_all_items copy severity from model_reply →
        booking item severity becomes 'routine' instead of 'p0', this fails.
        Mutation (disclosure_txt): copy from model_reply →
        disclosure_txt != catalog directive, this fails.
        """
        from agents.fi_fleet_health import writer

        stub_model_reply = {
            "severity": "routine",              # tries to downgrade p0 to routine
            "headline": "fabricated headline from model",
            "add_needs": ["fabricated_need"],   # tries to inject a spurious need
            "skip_grounding": True,             # tries to suppress the grounding item
            "disclosure_txt": "model-supplied paraphrase",
        }

        items = writer.build_all_items(
            vin=_VIN,
            attempt=1,
            open_faults=[{"code": _P0_CODE, "band": "p0",
                          "last_seen_at": "2026-10-01T00:00:00Z"}],
            catalog_entries={_P0_CODE: _P0_CATALOG_ENTRY},
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
            model_reply=stub_model_reply,
            candidate_dealer_ids=["dealer-meridian-001"],
        )
        booking = next(
            (i for i in items if i.get("item_type") == "booking"), None
        )
        grounding = next(
            (i for i in items
             if i.get("recommended_action", {}).get("action_kind") == "ground_vehicle"),
            None,
        )
        assert booking is not None, "Booking item must be present for a p0 vehicle."
        assert grounding is not None, (
            "Grounding item must be present for p0 with fault_grounding_enabled=True. "
            "Model reply skip_grounding=True must be ignored."
        )
        # Severity from band table, not model.
        assert booking.get("severity") == "p0", (
            "Model reply downgrading severity to 'routine' must be ignored. "
            "Mutation: copy severity from model_reply → 'routine', this fails."
        )
        assert booking.get("headline") != "fabricated headline from model", (
            "Headline must come from the fixed template, not the model reply. "
            "Mutation: copy headline from model_reply → mismatch with template, this fails."
        )
        # disclosure_txt on both items must be the catalog directive.
        assert booking.get("disclosure_txt") == _P0_CATALOG_ENTRY["directive"], (
            "Booking item: disclosure_txt must be the catalog directive (contract § 1.5). "
            "Mutation: copy from model_reply → paraphrase ≠ directive, this fails."
        )
        assert grounding.get("disclosure_txt") == _P0_CATALOG_ENTRY["directive"], (
            "Grounding item: disclosure_txt must be the catalog directive (contract § 1.5). "
            "Mutation: copy from model_reply → paraphrase ≠ directive, this fails."
        )
        # Needs list must not be polluted by the model reply.
        booking_needs = booking.get("needs", [])
        need_codes = [
            n if isinstance(n, str) else n.get("task_code", str(n))
            for n in booking_needs
        ]
        assert "fabricated_need" not in need_codes, (
            "Model reply adding 'fabricated_need' must be ignored. "
            "Mutation: copy add_needs from model_reply → fabricated_need appears, this fails."
        )


# ============================================================================
# Item 3 — Service-plan validator
# ============================================================================

class TestServicePlanValidator:
    """Spec D8 step 7: three rejection paths plus the one-retry rules fallback."""

    def test_rooftop_outside_candidates_rejected(self):
        """A dealer_id not in the candidate list is rejected."""
        from agents.fi_fleet_health.validator import ServicePlanValidator, ValidationError

        v = ServicePlanValidator(
            candidate_dealer_ids=["dealer-meridian-001", "dealer-meridian-002"],
            severity="soon",
        )
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-unknown-999", defer_need_ids=[], urgency="this_month")

    def test_deferred_p0_need_rejected(self):
        """A deferred p0 need is rejected (spec D8 step 7: deferrable needs only)."""
        from agents.fi_fleet_health.validator import ServicePlanValidator, ValidationError

        v = ServicePlanValidator(
            candidate_dealer_ids=["dealer-meridian-001"],
            severity="p0",
            non_deferrable_need_ids=["need-p0-001"],
        )
        with pytest.raises(ValidationError):
            v.validate(
                dealer_id="dealer-meridian-001",
                defer_need_ids=["need-p0-001"],
                urgency="this_week",
            )

    def test_deferred_attention_need_rejected(self):
        """A deferred attention need is rejected."""
        from agents.fi_fleet_health.validator import ServicePlanValidator, ValidationError

        v = ServicePlanValidator(
            candidate_dealer_ids=["dealer-meridian-001"],
            severity="attention",
            non_deferrable_need_ids=["need-att-001"],
        )
        with pytest.raises(ValidationError):
            v.validate(
                dealer_id="dealer-meridian-001",
                defer_need_ids=["need-att-001"],
                urgency="this_month",
            )

    def test_p0_urgency_only_this_week_allowed(self):
        """p0 severity allows only this_week; this_month is rejected."""
        from agents.fi_fleet_health.validator import ServicePlanValidator, ValidationError

        v = ServicePlanValidator(
            candidate_dealer_ids=["dealer-meridian-001"],
            severity="p0",
            non_deferrable_need_ids=[],
        )
        with pytest.raises(ValidationError):
            v.validate(
                dealer_id="dealer-meridian-001",
                defer_need_ids=[],
                urgency="this_month",
            )

    def test_p0_urgency_next_visit_rejected(self):
        """p0 severity does not allow next_visit either."""
        from agents.fi_fleet_health.validator import ServicePlanValidator, ValidationError

        v = ServicePlanValidator(
            candidate_dealer_ids=["dealer-meridian-001"],
            severity="p0",
            non_deferrable_need_ids=[],
        )
        with pytest.raises(ValidationError):
            v.validate(
                dealer_id="dealer-meridian-001",
                defer_need_ids=[],
                urgency="next_visit",
            )

    def test_one_retry_then_rules_fallback_makes_two_model_calls(self):
        """After one retry the rules fallback fires: exactly two model calls total."""
        from agents.fi_fleet_health.runner import run_with_validator_fallback

        call_count = [0]

        def failing_model_fn():
            call_count[0] += 1
            raise ValueError("validator rejected model output")

        result = run_with_validator_fallback(
            model_fn=failing_model_fn,
            rules_fn=lambda: {
                "diagnosis_source": "rules",
                "confidence": "low",
                "urgency": "this_week",
            },
            max_retries=1,
        )
        assert result["diagnosis_source"] == "rules"
        assert result["confidence"] == "low"
        assert call_count[0] == 2, (
            f"Expected 2 model calls (attempt + retry), got {call_count[0]}. "
            "Spec D8 step 7: one retry, then the rules fallback."
        )

    def test_rules_fallback_uses_nearest_candidate_urgency_from_band(self):
        """Rules fallback picks nearest candidate, urgency from band, nothing from model.

        Three candidate rooftops with different distances; the nearest is NOT first
        in the list, so a pass-through that picks candidates[0] would choose the
        wrong dealer_id.  Asserts the chosen dealer_id is the closest by distance_km.

        Mutation: pick the first candidate regardless of distance →
        dealer_id='dealer-far-001', this fails.
        """
        from agents.fi_fleet_health.runner import run_with_validator_fallback

        _CANDIDATES = [
            {"dealer_id": "dealer-far-001",     "distance_km": 85.0},  # far  — first in list
            {"dealer_id": "dealer-nearest-001", "distance_km": 12.3},  # nearest — not first
            {"dealer_id": "dealer-mid-001",     "distance_km": 40.0},  # mid-range
        ]

        def failing_model_fn():
            raise ValueError("model produced invalid urgency")

        result = run_with_validator_fallback(
            model_fn=failing_model_fn,
            rules_fn=lambda: {
                "diagnosis_source": "rules",
                "confidence": "low",
                "urgency": "this_week",   # from band p0
            },
            severity="p0",
            candidates=_CANDIDATES,
            max_retries=1,
        )
        assert result["diagnosis_source"] == "rules"
        assert result.get("urgency") == "this_week"
        assert result.get("dealer_id") == "dealer-nearest-001", (
            "Rules fallback must select the nearest rooftop by distance_km. "
            "The nearest (12.3 km) is not first — picking candidates[0] gives "
            "'dealer-far-001' (85.0 km). "
            "Mutation: pick first candidate → dealer_id='dealer-far-001', this fails."
        )
        # Fix W15: rules fallback must set defer_need_ids=[] (nothing deferred) and
        # the fixed detail text (spec D8 step 7).
        assert result.get("defer_need_ids") == [], (
            "Rules fallback must produce defer_need_ids=[] (nothing deferred). "
            "Mutation: omit defer_need_ids or carry a non-empty list → this fails."
        )
        assert result.get("detail") == _RULES_FALLBACK_DETAIL, (
            f"Rules fallback must produce the fixed detail text "
            f"{_RULES_FALLBACK_DETAIL!r}. "
            "Mutation: omit detail or allow model text → this fails."
        )


# ============================================================================
# Item 4 — Skip rule and cap; Athena selection pages through NextToken
# ============================================================================

class TestSkipRuleAndCap:
    """Spec D8 step 5: same-hash skip; cap 60; selection pages through NextToken."""

    def test_same_hash_model_written_skipped(self):
        """Same finding_hash + diagnosis_source=model → no model call, no DDB write."""
        from agents.fi_fleet_health.runner import HealthRunner

        written: list = []
        model_called: list = []

        runner = HealthRunner(
            write_fn=lambda f: written.append(f),
            model_fn=lambda v, ctx: model_called.append(ctx) or {"severity": "soon"},
        )
        existing_hash = "aa" * 32

        runner.maybe_write(
            vin=_VIN,
            finding_hash=existing_hash,
            diagnosis_source="model",
            existing_hash=existing_hash,
            severity="soon",
        )
        assert written == []
        assert model_called == []

    def test_same_hash_rules_written_model_called(self):
        """Same hash + diagnosis_source=rules → model is called (eligible for a model call)."""
        from agents.fi_fleet_health.runner import HealthRunner

        written: list = []
        model_called: list = []

        runner = HealthRunner(
            write_fn=lambda f: written.append(f),
            model_fn=lambda v, ctx: (model_called.append(ctx), {"severity": "soon"})[1],
        )
        existing_hash = "bb" * 32

        runner.maybe_write(
            vin=_VIN,
            finding_hash=existing_hash,
            diagnosis_source="rules",
            existing_hash=existing_hash,
            severity="soon",
        )
        assert len(written) == 1
        assert len(model_called) == 1, (
            "An item written by rules (diagnosis_source=rules) is eligible for a model "
            "call on the next run (spec D8 step 5)."
        )

    def test_apply_cap_trims_to_60(self):
        """apply_health_cap enforces the 60-item per-run cap."""
        from agents.fi_fleet_health.runner import apply_health_cap

        items = [{"vin": f"MRDVEH{i:012d}", "severity": "soon"} for i in range(70)]
        cap_items, over_cap = apply_health_cap(items, cap=60)
        assert len(cap_items) == 60

    def test_apply_cap_highest_severity_survives(self):
        """Items within the cap are the most severe (p0 then attention then soon then routine)."""
        from agents.fi_fleet_health.runner import apply_health_cap

        items = (
            [{"vin": f"MRDVEH{i:012d}", "severity": "routine"} for i in range(5)]
            + [{"vin": f"MRDVEH{i+5:012d}", "severity": "p0"} for i in range(3)]
        )
        cap_items, over_cap = apply_health_cap(items, cap=3)
        assert len(cap_items) == 3
        assert all(r["severity"] == "p0" for r in cap_items)

    def test_athena_selection_follows_next_token(self):
        """Fault-rows reader (repointed from vehicle_needs_view) pages via NextToken.

        The old vehicle_needs_view query is gone (task 3.2c).  The function under
        test is now select_vehicles_with_needs (shim), which pages the fault SQL
        via the callback-based athena_fn interface — NextToken is followed to
        exhaustion.

        Mutation guard (stop after first page): if the shim breaks out of the
        pagination loop after the first call, only page1_vins are returned and
        the set-equality assertion fails.
        """
        from agents.fi_fleet_health.selection import select_vehicles_with_needs

        page1_vins = [f"MRDVEH{i:012d}" for i in range(5)]
        page2_vins = [f"MRDVEH{i:012d}" for i in range(5, 10)]

        call_count = [0]
        pages = [
            {"ResultSet": {"Rows": [{"vin": v} for v in page1_vins]},
             "NextToken": "tok-page-2"},
            {"ResultSet": {"Rows": [{"vin": v} for v in page2_vins]},
             "NextToken": None},
        ]

        def fake_athena_query(sql, *args, **kwargs):
            # Verify the fault SQL is used (not the old vehicle_needs_view).
            assert "vehicle_needs_view" not in sql, (
                "select_vehicles_with_needs must not use the fake vehicle_needs_view SQL "
                "(task 3.2c repoint)."
            )
            assert "vehicle_fault_status" in sql, (
                "select_vehicles_with_needs must use the fault SQL after repointing."
            )
            idx = call_count[0]
            call_count[0] += 1
            return pages[idx] if idx < len(pages) else {"ResultSet": {"Rows": []}}

        result_vins = select_vehicles_with_needs(athena_fn=fake_athena_query)
        all_vins = page1_vins + page2_vins
        assert set(result_vins) == set(all_vins), (
            "selection must follow NextToken to exhaustion and return all pages."
        )
        assert call_count[0] == 2, (
            f"Expected 2 Athena calls (one per page), got {call_count[0]}."
        )


# ============================================================================
# Item 5 — Attempt rules for both subject forms
# ============================================================================

class TestAttemptRules:
    """Spec D9 / contract § 6: attempt rules for service/<n> and ground/<n>."""

    def test_cancelled_triggers_next_attempt_immediately(self):
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="cancelled",
            severity="soon",
            days_since_decision=0,
        ) is True

    def test_failed_triggers_next_attempt_immediately(self):
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="failed",
            severity="p0",
            days_since_decision=0,
        ) is True

    def test_refused_triggers_next_attempt_immediately(self):
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="refused",
            severity="attention",
            days_since_decision=0,
        ) is True

    def test_rejected_p0_within_14_days_no_retry(self):
        """Operator-rejected p0/attention: next attempt only after 14 days."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="rejected",
            severity="p0",
            days_since_decision=10,
        ) is False

    def test_rejected_p0_after_14_days_retries(self):
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="rejected",
            severity="p0",
            days_since_decision=14,
        ) is True

    def test_rejected_soon_within_30_days_no_retry(self):
        """Below attention: next attempt after 30 days."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="rejected",
            severity="soon",
            days_since_decision=20,
        ) is False

    def test_rejected_soon_after_30_days_retries(self):
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="rejected",
            severity="soon",
            days_since_decision=30,
        ) is True

    def test_blocked_guardrail_no_retry(self):
        """blocked (guardrail fire) → no automatic retry, regardless of severity."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="blocked",
            severity="p0",
            days_since_decision=0,
        ) is False

    def test_stalled_dispatch_no_retry(self):
        """stalled (dispatch timed out) → no attempt item."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="stalled",
            severity="attention",
            days_since_decision=0,
        ) is False

    def test_executed_no_retry(self):
        """executed → no further attempt."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="executed",
            severity="soon",
            days_since_decision=100,
        ) is False

    def test_after_5_attempts_informational_item_written(self):
        """After 5 attempts an informational item (no recommended_action) is written."""
        from agents.fi_fleet_health.attempts import build_max_attempts_item

        item = build_max_attempts_item(
            vin=_VIN,
            attempt=6,
            subject_segment="service",
        )
        assert "recommended_action" not in item
        assert item.get("severity") == "informational", (
            "After 5 attempts the item severity must be 'informational'. "
            "Mutation: severity='soon' → this fails."
        )
        assert "follow-up needed" in item.get("headline", "").lower() or \
               "fail" in item.get("headline", "").lower()

    def test_attempt_rules_apply_to_grounding_subject_form(self):
        """Attempt rules apply equally to the grounding subject form (ground/<n>)."""
        from agents.fi_fleet_health.attempts import should_retry_health

        # grounding items use the same severity/timing rules as booking items
        assert should_retry_health(
            prev_status="cancelled",
            severity="p0",
            days_since_decision=0,
            subject_segment="ground",
        ) is True


# ============================================================================
# Item 6 — fault_grounding_enabled false → no grounding item
# ============================================================================

class TestGroundingFlag:
    """spec D9 rollout flag: fault_grounding_enabled=False suppresses the grounding item."""

    def test_grounding_disabled_p0_booking_only(self):
        """With fault_grounding_enabled=False a p0 vehicle gets booking but no grounding."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=False,
        )
        assert result.severity == "p0"
        assert "booking" in result.items
        assert "grounding" not in result.items

    def test_grounding_disabled_booking_severity_unchanged(self):
        """Disabling the flag does not change the booking item's severity."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=False,
        )
        assert result.severity == "p0", (
            "The booking item must still be p0; the flag suppresses grounding only."
        )

    def test_grounding_disabled_disclosure_txt_still_set(self):
        """The p0 booking item still carries disclosure_txt even when grounding is off."""
        from agents.fi_fleet_health import writer

        # When grounding is disabled the booking item must still surface the stop-driving text.
        txt = writer.build_disclosure_txt(fault_catalog_entry=_P0_CATALOG_ENTRY)
        assert txt == _P0_CATALOG_ENTRY["directive"]

    def test_grounding_enabled_after_flag_written(self):
        """With flag=True the grounding item is back."""
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            fault_grounding_enabled=True,
        )
        assert "grounding" in result.items



# ============================================================================
# Fix W15 — Missing / tautological fleet-health tests
# ============================================================================

class TestGroundingFlagDefault:
    """W15 fix: fault_grounding_enabled must default to False (spec D9 rollout flag)."""

    def test_grounding_enabled_default_is_false(self):
        """When fault_grounding_enabled is not passed it must default to False.

        Mutation: default to True → grounding item appears without the flag, this fails.
        """
        from agents.fi_fleet_health import bands

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            already_grounded=False,
            # fault_grounding_enabled omitted intentionally
        )
        assert result.severity == "p0"
        assert "booking" in result.items
        assert "grounding" not in result.items, (
            "fault_grounding_enabled must default to False; no grounding item without "
            "explicit True (spec D9 rollout flag). "
            "Mutation: default to True → grounding item present, this fails."
        )

    def test_groundings_read_error_treated_as_not_grounded(self):
        """A groundings read error means the vehicle is treated as not grounded (D8 step 3).

        Fail-closed: when the CMS groundings endpoint is unavailable the run must
        still produce a grounding item for a p0 vehicle with the flag on.
        Mutation: treat an error as already_grounded=True → grounding item suppressed,
        this fails.
        """
        from agents.fi_fleet_health import bands

        def _failing_reader(vin: str) -> bool:
            raise Exception("CMS groundings unavailable")

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            grounding_reader_fn=_failing_reader,
            fault_grounding_enabled=True,
        )
        assert result.severity == "p0"
        assert "grounding" in result.items, (
            "A groundings read error must be treated as not grounded (D8 step 3). "
            "Mutation: treat error as already_grounded=True → no grounding item, this fails."
        )


class TestAttemptCapAndBoundary:
    """W15 fix: 5-attempt cap; attention 14-day boundary; model-call cap."""

    def test_attempt_5_returns_false_cap_reached(self):
        """After 5 attempts should_retry_health returns False (5-attempt cap, spec D9).

        Mutation: ignore the attempt parameter → True returned, this fails.
        """
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="cancelled",
            severity="p0",
            days_since_decision=0,
            attempt=5,
        ) is False, (
            "5-attempt cap: should_retry_health must return False at attempt=5. "
            "Mutation: ignore attempt → True, this fails."
        )

    def test_attempt_4_still_retries(self):
        """Before the 5-attempt cap retries continue (attempt=4 is the 4th; one left)."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="cancelled",
            severity="p0",
            days_since_decision=0,
            attempt=4,
        ) is True

    def test_rejected_attention_within_14_days_no_retry(self):
        """Operator-rejected attention: next attempt only after 14 days (spec D9).

        Mutation: treat attention as soon (30-day window) → True at day 13, this fails.
        """
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="rejected",
            severity="attention",
            days_since_decision=13,
        ) is False, (
            "Attention uses the 14-day window same as p0. "
            "Mutation: use 30 days for attention → True at day 13, this fails."
        )

    def test_rejected_attention_after_14_days_retries(self):
        """Operator-rejected attention: retry allowed after 14 days."""
        from agents.fi_fleet_health.attempts import should_retry_health

        assert should_retry_health(
            prev_status="rejected",
            severity="attention",
            days_since_decision=14,
        ) is True

    def test_over_cap_p0_vehicle_gets_rules_path_item(self):
        """A p0 vehicle past the 60-model-call cap still gets a written item on the rules path.

        D8's cap is on model calls, not on item writes. Over-cap vehicles must still
        receive a Finding via the rules path.
        Mutation: drop over-cap vehicles entirely → 5 p0 vehicles get no item, this fails.
        """
        from agents.fi_fleet_health.runner import apply_health_cap, write_over_cap_items

        p0_items = [
            {"vin": f"MRDVEH{i:012d}", "severity": "p0",
             "diagnosis_source": "model", "finding_kind": None}
            for i in range(65)
        ]
        cap_items, over_cap = apply_health_cap(p0_items, cap=60)
        assert len(over_cap) == 5, (
            "apply_health_cap must return the 5 over-cap items separately, not discard them."
        )
        written = write_over_cap_items(over_cap)
        assert len(written) == 5, (
            "All 5 over-cap items must be written on the rules path. "
            "Mutation: return [] from write_over_cap_items → len=0, this fails."
        )
        for item in written:
            assert item.get("diagnosis_source") == "rules", (
                "Over-cap items must be written with diagnosis_source=rules. "
                "Mutation: leave as 'model' or drop → this fails."
            )

    def test_rules_fallback_urgency_corrected_by_band_not_passed_through(self):
        """Rules fallback corrects urgency to the band constraint, not just passes rules_fn.

        Fix W15 item 6: the rules-fallback tests were tautological — they asserted the
        value their own rules_fn returned. This test uses a rules_fn that proposes an
        urgency the band forbids for a p0 vehicle, and asserts the urgency is corrected.
        Mutation: pass rules_fn urgency through without band validation →
        urgency=this_month for a p0 vehicle, this fails.
        """
        from agents.fi_fleet_health.runner import run_with_validator_fallback

        def failing_model_fn():
            raise ValueError("model output invalid")

        # rules_fn proposes this_month for a p0 vehicle — band must correct to this_week.
        result = run_with_validator_fallback(
            model_fn=failing_model_fn,
            rules_fn=lambda: {
                "diagnosis_source": "rules",
                "confidence": "low",
                "dealer_id": "dealer-meridian-001",
                "urgency": "this_month",   # wrong for p0 — band permits only this_week
            },
            severity="p0",
            max_retries=1,
        )
        assert result["diagnosis_source"] == "rules"
        assert result.get("urgency") == "this_week", (
            "For a p0 vehicle the fallback urgency must be this_week (the only band-allowed "
            "urgency), not the this_month value rules_fn returned. "
            "Mutation: pass urgency through without band check → this_month, this fails."
        )

        # Fix W15: rules fallback must set defer_need_ids=[] and the fixed detail text
        # (spec D8 step 7). These were unasserted in the prior cycle.
        assert result.get("defer_need_ids") == [], (
            "Rules fallback must produce defer_need_ids=[] (spec D8 step 7: 'nothing "
            "deferred'). Mutation: omit defer_need_ids or include a non-empty list → "
            "this fails."
        )
        assert result.get("detail") == _RULES_FALLBACK_DETAIL, (
            f"Rules fallback must produce the fixed detail {_RULES_FALLBACK_DETAIL!r} "
            "(spec D8 step 7: 'fixed detail'). "
            "Mutation: omit detail or let model text through → this fails."
        )


class TestRealGroundingsReader:
    """W15: drive grounding.py's real reader with a DDB client double.

    The old fixture mocked ddb_resource.Table.get_item, which Stage C's
    check_grounded never calls — it uses ddb_client.query. Fix: mock
    ddb_client.query (the real call) and use keyword ddb_client=mock_ddb.
    """

    def test_real_groundings_reader_client_error_is_not_grounded(self):
        """grounding.make_groundings_reader with a failing ddb_client.query is treated
        as not grounded (D8 step 3 fail-closed semantics).

        Mutation: make_groundings_reader returns True (already grounded) on
        ClientError → grounding item absent → 'grounding' not in result.items →
        this assertion fails.
        """
        from botocore.exceptions import ClientError
        from unittest.mock import MagicMock
        from agents.fi_fleet_health import bands, grounding

        mock_ddb = MagicMock()
        # check_grounded calls ddb_client.query() — mock the real call.
        mock_ddb.query.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException",
                       "Message": "CMS groundings table access denied"}},
            "Query",
        )
        reader = grounding.make_groundings_reader(
            ddb_client=mock_ddb,
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicle_id="veh-001",
        )

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            grounding_reader_fn=reader,
            fault_grounding_enabled=True,
        )
        assert "grounding" in result.items, (
            "A DDB ClientError on the groundings read must be treated as not grounded "
            "(D8 step 3: read error → safe direction = vehicle not grounded → "
            "grounding item must be written). "
            "Mutation: make_groundings_reader returns True on ClientError → "
            "already_grounded=True → grounding item absent → this fails."
        )

    def test_real_groundings_reader_open_grounding_suppresses_grounding_item(self):
        """Success case: an open grounding in the table → already_grounded=True
        → no grounding item written (contract § 1.3: no second grounding item
        when vehicle is already grounded).
        """
        from unittest.mock import MagicMock
        from agents.fi_fleet_health import bands, grounding

        mock_ddb = MagicMock()
        # check_grounded returns non-empty Items → vehicle is already grounded.
        mock_ddb.query.return_value = {
            "Items": [{"vehicleId": {"S": "veh-001"}}],
        }
        reader = grounding.make_groundings_reader(
            ddb_client=mock_ddb,
            groundings_table="cms-staging-storage-vehicle-groundings",
            vehicle_id="veh-001",
        )

        result = bands.classify_needs(
            open_faults=[{"code": _P0_CODE, "band": "p0"}],
            agent2_findings=[],
            pm_tasks=[],
            grounding_reader_fn=reader,
            fault_grounding_enabled=True,
        )
        assert result.severity == "p0"
        assert "grounding" not in result.items, (
            "An open grounding in the table means the vehicle is already grounded; "
            "no second grounding item should be produced (contract § 1.3)."
        )
        assert "booking" in result.items



# ============================================================================
# Schema validation — Built Findings must validate against finding.schema.json
# ============================================================================

class TestFindingSchemaValidation:
    """Built fleet-health Findings must validate against finding.schema.json.

    Instruction from task 3.2: 'Built Findings must validate against
    agents/tier2/schemas/finding.schema.json (add that test).'
    """

    def test_booking_finding_validates_against_schema(self):
        """A p0 booking Finding built by write.build_finding validates against the schema."""
        import json
        import uuid
        from pathlib import Path
        import jsonschema
        from agents.fi_fleet_health.write import build_finding, AGENT_ID

        schema_path = Path(__file__).parent.parent.parent.parent / (
            "agents/tier2/schemas/finding.schema.json"
        )
        schema = json.loads(schema_path.read_text())

        item = {
            "item_type": "booking",
            "severity": "p0",
            "subject_key": "service/1",
            "headline": f"Stop-driving fault {_P0_CODE}: book service at dealer-meridian-001",
            "detail": "Service plan from deterministic rules: nearest candidate selected.",
            "disclosure_txt": _P0_CATALOG_ENTRY["directive"],
            "recommended_action": {
                "action_kind": "book_fleet_service",
                "target_system": "dms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "needs": [],
                    "dealer_id": "dealer-meridian-001",
                    "urgency": "this_week",
                },
            },
        }

        finding = build_finding(
            vin=_VIN,
            fleet_id=_FLEET_ID,
            item=item,
            computed_at="2026-10-05T06:00:00Z",
            inputs_hash="a" * 64,
            evidence=[],
            confidence="low",
        )

        # Must validate without raising ValidationError.
        jsonschema.validate(finding, schema)
        assert finding["agent_id"] == AGENT_ID
        assert finding["finding_kind"] == "fleet.health.service"
        assert finding["scope"]["vin"] == _VIN

    def test_grounding_finding_validates_against_schema(self):
        """A grounding Finding (severity=p0) validates against the schema."""
        import json
        from pathlib import Path
        import jsonschema
        from agents.fi_fleet_health.write import build_finding

        schema_path = Path(__file__).parent.parent.parent.parent / (
            "agents/tier2/schemas/finding.schema.json"
        )
        schema = json.loads(schema_path.read_text())

        item = {
            "item_type": "grounding",
            "severity": "p0",
            "subject_key": "ground/1",
            "headline": f"Stop-driving fault {_P0_CODE}: ground until the repair is recorded",
            "detail": "",
            "disclosure_txt": _P0_CATALOG_ENTRY["directive"],
            "recommended_action": {
                "action_kind": "ground_vehicle",
                "target_system": "cms",
                "parameters": {
                    "vehicle_vin": _VIN,
                    "fault_codes": [_P0_CODE],
                },
            },
        }

        finding = build_finding(
            vin=_VIN,
            fleet_id=_FLEET_ID,
            item=item,
            computed_at="2026-10-05T06:00:00Z",
            inputs_hash="b" * 64,
            evidence=[],
            confidence="low",
        )

        jsonschema.validate(finding, schema)
        assert finding["finding_kind"] == "fleet.health.service"
        assert finding["disclosure_txt"] == _P0_CATALOG_ENTRY["directive"]
