"""Red tests — FI recall and warranty writer, validator, runner and pagination.

Covers task 1.4 items 1–7 (item 11 is deferred per decisions.md 2026-10-05):

  1. Band table (spec D9): five rows, severity and items; grounding never from model output.
  2. disclosure_txt byte-equal to catalog text; headlines only from the three templates.
  3. Recall validator: dealer outside candidates, combine_with outside bundle, urgency the
     band forbids — each rejected; after one retry the rules fallback fires.
  4. Warranty: estimated_value_usd equals DMS's recoverable_usd; insufficient_evidence
     writes an item with no recommended_action; denied lines produce no item.
  5. Skip rule: same hash + model-written → no call, no write; same hash + rules-written →
     eligible, sorted highest severity first, within cap (60 recall, 30 warranty).
  6. Attempts (D12): cancelled/failed/refused → attempt n+1; rejected/safety → 14 days;
     rejected/non-safety → 30 days; stalled → none; after 5 attempts informational item.
  7. Athena selection follows NextToken to the end (stub returning three pages).

Every test is xfail(strict=True, reason="FI stage C task 1.4"). All not-yet-built module
imports are inside test bodies so collection never errors. Use CSV custom:fleetIds in
fixtures.

Run:
    .venv/bin/python -m pytest agents/fi_recall_warranty/tests -q
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Fixtures shared across items
# ---------------------------------------------------------------------------

#: Representative Meridian VIN; all VINs use MRD prefix per spec.
_VIN = "MRDVEH00000000015"
_FLEET_ID = "fleet-meridian-01"

#: CSV custom:fleetIds claim per task constraint.
_CLAIMS_OPERATOR = {
    "sub": "a1b2c3d4-0000-4000-8000-000000000001",
    "cognito:groups": ["fleet-operator"],
    "custom:fleetIds": f"{_FLEET_ID},fleet-meridian-02",  # CSV, not a list
}

#: A minimal recall catalog entry with the three required text fields.
_CATALOG_ENTRY = {
    "campaign_id": "MRD-2024-001",
    "recall_class": "safety",
    "do_not_drive": True,
    "summary": "The brake caliper may seize due to a manufacturing defect.",
    "consequence": "Reduced braking ability increases risk of a crash.",
    "remedy": "Dealers will replace the affected brake caliper at no charge.",
    "remedy_available_from": None,
}

#: Expected disclosure separator pinned here so every test uses the same definition.
#: Contract § 1.5 (decisions.md call 12): summary + consequence + remedy joined by "\n\n",
#: byte for byte. The trim test must fail on a trim or a paraphrase.
_DISCLOSURE_SEPARATOR = "\n\n"


# ============================================================================
# Item 1 — Band table
# ============================================================================

class TestBandTable:
    """Spec D9: five deterministic rows. Model output cannot alter the results."""

    def test_row1_safety_do_not_drive_either_basis_p0_ground_book(self):
        """Row 1: safety + do_not_drive + CONFIRMED → p0, [ground, book]."""
        from agents.fi_recall_warranty import band

        result = band.classify(
            recall_class="safety",
            do_not_drive=True,
            match_basis="CONFIRMED",
        )
        assert result.severity == "p0"
        assert "ground" in result.items
        assert "book" in result.items

    def test_row1_safety_do_not_drive_population_basis_p0_ground_book(self):
        """Row 1: safety + do_not_drive + POPULATION → p0, [ground, book] (either basis)."""
        from agents.fi_recall_warranty import band

        result = band.classify(
            recall_class="safety",
            do_not_drive=True,
            match_basis="POPULATION",
        )
        assert result.severity == "p0"
        assert "ground" in result.items
        assert "book" in result.items

    def test_row2_safety_no_do_not_drive_confirmed_p0_ground_book(self):
        """Row 2: safety + not do_not_drive + CONFIRMED → p0, [ground, book]."""
        from agents.fi_recall_warranty import band

        result = band.classify(
            recall_class="safety",
            do_not_drive=False,
            match_basis="CONFIRMED",
        )
        assert result.severity == "p0"
        assert "ground" in result.items
        assert "book" in result.items

    def test_row3_safety_no_do_not_drive_population_attention_book_only(self):
        """Row 3: safety + not do_not_drive + POPULATION → attention, [book] only."""
        from agents.fi_recall_warranty import band

        result = band.classify(
            recall_class="safety",
            do_not_drive=False,
            match_basis="POPULATION",
        )
        assert result.severity == "attention"
        assert result.items == ["book"]
        assert "ground" not in result.items

    def test_row4_non_safety_confirmed_soon_book_only(self):
        """Row 4: non_safety + CONFIRMED → soon, [book] only."""
        from agents.fi_recall_warranty import band

        result = band.classify(
            recall_class="non_safety",
            do_not_drive=False,
            match_basis="CONFIRMED",
        )
        assert result.severity == "soon"
        assert result.items == ["book"]

    def test_row5_non_safety_population_routine_book_only(self):
        """Row 5: non_safety + POPULATION → routine, [book] only."""
        from agents.fi_recall_warranty import band

        result = band.classify(
            recall_class="non_safety",
            do_not_drive=False,
            match_basis="POPULATION",
        )
        assert result.severity == "routine"
        assert result.items == ["book"]

    def test_model_hint_cannot_remove_grounding_from_p0_item(self):
        """Grounding is never taken from model output (spec D9).

        A fake model reply that asks to skip grounding for a do_not_drive p0
        vehicle must have no effect on the band result.
        """
        from agents.fi_recall_warranty import band

        # Model hint asking to skip grounding — must be ignored.
        fake_model_hint = {"skip_ground": True, "severity": "routine"}
        result = band.classify(
            recall_class="safety",
            do_not_drive=True,
            match_basis="CONFIRMED",
            model_hint=fake_model_hint,
        )
        assert result.severity == "p0"
        assert "ground" in result.items

    def test_model_hint_cannot_add_grounding_to_non_p0_item(self):
        """Model output requesting a grounding on a non-p0 row is also ignored."""
        from agents.fi_recall_warranty import band

        fake_model_hint = {"add_ground": True}
        result = band.classify(
            recall_class="safety",
            do_not_drive=False,
            match_basis="POPULATION",
            model_hint=fake_model_hint,
        )
        # Row 3: attention, book only — no grounding regardless of model hint.
        assert result.severity == "attention"
        assert "ground" not in result.items

    def test_writer_cannot_add_grounding_to_band_result(self):
        """Security review area B W1: the model can't ADD a grounding either (spec D9:
        "neither remove nor add"). Row 4 (safety, not do-not-drive, POPULATION) is book-only;
        a model plan asking to ground must not change that."""
        from agents.fi_recall_warranty import band, writer

        band_result = band.classify(
            recall_class="safety",
            do_not_drive=False,
            match_basis="POPULATION",
        )
        model_plan = {
            "items": ["ground", "book"],
            "ground": True,
            "dealer_id": "dealer-seattle",
            "urgency": "this_month",
            "detail": "Ground it to be safe.",
            "combine_with": [],
        }
        items = writer.build_items_from_band(band_result=band_result, model_plan=model_plan)
        assert items == ["book"], f"Writer must not add a grounding; got {items!r}"

    def test_writer_cannot_remove_grounding_from_band_result(self):
        """Writer-level seam: model_plan cannot override the items the band assigned.

        The property belongs to the writer (Warning, checklist § grounding test).
        The fake model reply carries an 'items' key that contradicts the band result
        (book-only, dropping 'ground'). The writer must still emit the band's items
        unchanged — a pipeline that reads model_plan["items"] must fail this test.

        Mutation N3: writer.build_items_from_band returns model_plan.get("items",
        band_result.items) → items == ["book"] (missing "ground") → assert fails.
        """
        from agents.fi_recall_warranty import band, writer

        band_result = band.classify(
            recall_class="safety",
            do_not_drive=True,
            match_basis="CONFIRMED",
        )
        # band_result.items == ["ground", "book"] (Row 1).

        # Model plan carrying items that contradict the band — must be ignored.
        model_plan = {
            "items": ["book"],          # contradicts band: tries to drop "ground"
            "ground": False,            # another form of contradiction — also ignored
            "dealer_id": "dealer-seattle",
            "urgency": "this_week",
            "detail": "Book at the nearest dealer as soon as possible.",
            "combine_with": [],
        }
        items = writer.build_items_from_band(band_result=band_result, model_plan=model_plan)
        expected = ["ground", "book"]  # Band Row 1 (p0, do_not_drive=True) is deterministic
        assert items == expected, (
            f"Writer must return band_result.items unchanged regardless of model_plan. "
            f"Expected {expected!r}, got {items!r}. "
            "N7 check: mutation that reassigns band_result.items before returning must fail this.\n"
            "(W4 fix: expected is the concrete Row-1 value, not read from band_result.items after\n"
            "the call — N7 mutates band_result.items in place, so reading it afterwards would yield\n"
            "the mutated value and the assertion would vacuously pass.)"
        )


# ============================================================================
# Item 2 — disclosure_txt and headline templates
# ============================================================================

class TestDisclosureAndHeadlines:
    """disclosure_txt is byte-equal to catalog text; headlines from the three booking templates.

    Contract § 1.4: three booking/grounding templates. The after-5-attempts informational
    item uses its own headline ("Bookings keep failing — manual follow-up needed") which is
    not a § 1.4 template (no `recommended_action`).
    Contract § 1.5: disclosure_txt = summary + consequence + remedy, byte for byte, no
    delimiter.
    """

    def test_disclosure_txt_is_summary_consequence_remedy_concatenated(self):
        """disclosure_txt = summary + '\n\n' + consequence + '\n\n' + remedy, byte for byte.

        Contract § 1.5 (decisions.md call 12): the three fields are joined by "\n\n".
        Pinned separator: _DISCLOSURE_SEPARATOR = "\n\n".
        """
        from agents.fi_recall_warranty import writer

        txt = writer.build_disclosure_txt(_CATALOG_ENTRY)
        expected = (
            _CATALOG_ENTRY["summary"]
            + _DISCLOSURE_SEPARATOR
            + _CATALOG_ENTRY["consequence"]
            + _DISCLOSURE_SEPARATOR
            + _CATALOG_ENTRY["remedy"]
        )
        assert txt == expected

    def test_disclosure_txt_mutation_trim_fails(self):
        """Mutation guard: a paraphrase or a trim must not equal the full text.

        M8: build_disclosure_txt returns a paraphrase of the catalog text.
        M8t: build_disclosure_txt drops the remedy section.

        Contract § 1.5 (decisions.md call 12): all three fields must be present,
        joined by "\\n\\n". The test asserts structural membership, not just length:
        summary in txt, consequence in txt, and txt ends with remedy.
        """
        from agents.fi_recall_warranty import writer

        txt = writer.build_disclosure_txt(_CATALOG_ENTRY)
        summary = _CATALOG_ENTRY["summary"]
        consequence = _CATALOG_ENTRY["consequence"]
        remedy = _CATALOG_ENTRY["remedy"]

        # Structural membership guards (catch both M8 paraphrase and M8t trim):
        assert summary in txt, (
            "disclosure_txt must contain the catalog summary byte for byte."
        )
        assert consequence in txt, (
            "disclosure_txt must contain the catalog consequence byte for byte."
        )
        assert txt.endswith(remedy), (
            "disclosure_txt must end with the catalog remedy byte for byte. "
            "M8t: a trim that drops the remedy fails this check."
        )

    def test_headline_grounding_fixed_template(self):
        """Grounding headline: 'Safety recall <id>: ground until the remedy is done'."""
        from agents.fi_recall_warranty import writer

        headline = writer.build_headline(
            item_type="ground",
            campaign_id="MRD-2024-001",
            dealer_id=None,
            remedy_date=None,
        )
        assert headline == "Safety recall MRD-2024-001: ground until the remedy is done"

    def test_headline_booking_remedy_available_fixed_template(self):
        """Booking headline (parts available): 'Recall <id>: book the remedy at <rooftop>'."""
        from agents.fi_recall_warranty import writer

        headline = writer.build_headline(
            item_type="book",
            campaign_id="MRD-2024-001",
            dealer_id="dealer-seattle",
            remedy_date=None,
        )
        assert headline == "Recall MRD-2024-001: book the remedy at dealer-seattle"

    def test_headline_booking_remedy_not_yet_available_fixed_template(self):
        """parts_expected headline: 'Recall <id>: remedy parts expected <date>'."""
        from agents.fi_recall_warranty import writer

        headline = writer.build_headline(
            item_type="parts_expected",
            campaign_id="MRD-2024-001",
            dealer_id=None,
            remedy_date="2025-06-01",
        )
        assert headline == "Recall MRD-2024-001: remedy parts expected 2025-06-01"

    def test_no_fourth_headline_template_exists(self):
        """Only the three § 1.4 templates are valid; an unknown item_type raises.

        The after-5-attempts informational item uses its own headline shape
        (see build_max_attempts_item) and is not a fourth § 1.4 template.
        """
        from agents.fi_recall_warranty import writer

        with pytest.raises((ValueError, KeyError)):
            writer.build_headline(
                item_type="unknown_type",
                campaign_id="MRD-2024-001",
                dealer_id=None,
                remedy_date=None,
            )


# ============================================================================
# Item 3 — Recall validator
# ============================================================================

class TestRecallValidator:
    """Three rejection paths plus the one-retry rules fallback (contract § 1.7, spec D9).

    decisions.md 2026-10-05 call 9: "one retry" means two model calls.
    """

    def test_dealer_outside_candidates_rejected(self):
        """A dealer not in the candidate list is rejected."""
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle", "dealer-portland"],
        )
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-denver", combine_with=None, urgency=None)

    def test_combine_with_outside_bundle_rejected(self):
        """A combine_with id whose Finding is not in the bundle is rejected.

        combine_with is a list of Finding ids from the vehicle's open items
        (contract § 1.6 vehicle.open_items). Each id must be in the bundle.
        A single id that is outside the bundle is rejected.
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=["MRD-2024-001"],
        )
        # A list with one id inside and one outside the bundle.
        with pytest.raises(ValidationError):
            v.validate(
                dealer_id="dealer-seattle",
                combine_with=["MRD-2024-001", "MRD-9999-999"],
                urgency=None,
            )

    def test_combine_with_single_item_outside_bundle_rejected(self):
        """A single combine_with id not in the bundle is rejected."""
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=["MRD-2024-001"],
        )
        with pytest.raises(ValidationError):
            v.validate(
                dealer_id="dealer-seattle",
                combine_with=["MRD-9999-999"],
                urgency=None,
            )

    def test_urgency_p0_only_this_week_allowed(self):
        """p0 allows this_week only; this_month is rejected (band rule, not just enum check)."""
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["p0"],
        )
        # p0 + this_month must be rejected (only this_week is allowed at p0).
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="this_month")

    def test_urgency_safety_attention_next_visit_rejected(self):
        """Safety `attention` may not use `next_visit` (band rule).

        The band rule: safety items may not use next_visit (only non-safety can).
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        # safety + attention — next_visit is not allowed for any safety severity.
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["attention"],
            recall_class="safety",
        )
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="next_visit")

    def test_urgency_non_safety_next_visit_accepted(self):
        """Non-safety `routine` with next_visit is accepted (band allows it)."""
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["routine"],
            recall_class="non_safety",
        )
        # Should not raise
        v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="next_visit")

    def test_urgency_p0_this_week_is_accepted(self):
        """p0 + this_week is accepted (the only allowed urgency at p0).

        N1 boundary: a validator that rejects ALL urgency at p0 (empty _P0_ALLOWED_URGENCY)
        fails this test. this_week must be accepted.
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["p0"],
        )
        # Must not raise.
        v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="this_week")

    def test_urgency_safety_attention_this_month_is_accepted(self):
        """Safety `attention` + this_month is accepted (D8 step 6 forbids only next_visit).

        D8 step 6: 'next_visit' is the only forbidden urgency for 'other safety items'
        (rows 2–3: safety attention). this_month is allowed.

        N8 boundary: a validator that adds this_month to _SAFETY_FORBIDDEN_URGENCY
        rejects this combination → all tests pass vacuously. This test catches that mutation.
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["attention"],
            recall_class="safety",
        )
        # safety + attention + this_month must NOT raise.
        v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="this_month")

    def test_empty_candidate_list_rejects_any_dealer(self):
        """Empty candidate list means no dealer is valid — any dealer is rejected.

        N2 fail-open guard: `if self.candidate_dealer_ids and dealer not in ...` disables
        the check when the list is empty. The validator must be fail-closed: an empty
        candidate list means the caller has no valid candidates and any dealer is rejected.
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=[],  # empty — no valid candidates
            bundle_ids=["MRD-2024-001"],
        )
        with pytest.raises(ValidationError, match="candidate"):
            v.validate(dealer_id="dealer-invented", combine_with=None, urgency=None)

    def test_empty_bundle_rejects_nonempty_combine_with(self):
        """Empty bundle list means no Finding is valid to combine with — any id is rejected.

        N2 fail-open guard: `if self.bundle_ids and id not in ...` disables the combine_with
        check when the list is empty. The validator must be fail-closed: an empty bundle
        means no valid combine_with ids exist, so any non-empty combine_with is rejected.
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=[],  # empty — no valid combine_with ids
        )
        with pytest.raises(ValidationError, match="combine_with"):
            v.validate(
                dealer_id="dealer-seattle",
                combine_with=["AVX-FND-invented"],
                urgency=None,
            )

    def test_combine_with_fully_inside_bundle_is_accepted(self):
        """combine_with list whose ids are all inside the bundle is accepted.

        N6 boundary: a validator that rejects every non-empty combine_with list
        fails this test. An id that IS in the bundle must be accepted.
        """
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError

        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=["MRD-2024-001", "MRD-2024-002"],
        )
        # All ids are inside the bundle — must not raise.
        v.validate(
            dealer_id="dealer-seattle",
            combine_with=["MRD-2024-001"],
            urgency=None,
        )

    def test_one_retry_then_rules_fallback_makes_two_model_calls(self):
        """After one retry the rules fallback fires: exactly two model calls.

        decisions.md 2026-10-05 call 9: "one retry" means two model calls.
        Fallback result must carry diagnosis_source='rules' and confidence='low'.
        rules_fn receives the last exception (D8 step 6: "the error passed to the retry").
        """
        from agents.fi_recall_warranty.runner import run_with_validator_fallback

        call_count = [0]

        def failing_model_fn(prev_error=None):
            call_count[0] += 1
            raise ValueError("model validation failed")

        result = run_with_validator_fallback(
            model_fn=failing_model_fn,
            rules_fn=lambda _err: {"diagnosis_source": "rules", "confidence": "low"},
            max_retries=1,
        )
        assert result["diagnosis_source"] == "rules"
        assert result["confidence"] == "low"
        assert call_count[0] == 2, (
            f"Expected 2 model calls (one attempt + one retry), got {call_count[0]}. "
            "decisions.md 2026-10-05 call 9: 'one retry' means two model calls."
        )

    def test_rules_fallback_receives_model_error(self):
        """D8 step 6: the error from the last model failure is passed to rules_fn.

        rules_fn is called with the last exception so it can include the error in
        the fallback detail. A runner that calls rules_fn() with no arguments fails
        this check (the error is lost and the rules_fn cannot report it).
        """
        from agents.fi_recall_warranty.runner import run_with_validator_fallback

        captured_error = [None]

        def failing_model_fn(prev_error=None):
            raise ValueError("model-failed: urgency constraint violated")

        def capture_rules(error):
            captured_error[0] = error
            return {
                "diagnosis_source": "rules",
                "confidence": "low",
                "urgency": "this_week",
                "combine_with": [],
                "detail": "Rules fallback: book at the nearest available dealer.",
            }

        run_with_validator_fallback(
            model_fn=failing_model_fn,
            rules_fn=capture_rules,
            max_retries=1,
        )
        assert isinstance(captured_error[0], ValueError), (
            "D8 step 6: rules_fn must be called with the last model exception. "
            "A runner that calls rules_fn() with no arguments leaves captured_error[0]=None."
        )
        assert "model-failed" in str(captured_error[0]), (
            "rules_fn must receive the actual exception from the last model call."
        )

    def test_retry_receives_first_model_exception(self):
        """The retry (second model call) receives the first call's exception.

        D8 step 6: 'one retry with the error'. A runner that calls model_fn() twice
        without passing the previous exception fails this check — the retry prompt
        cannot describe the constraint that failed.

        N-retry guard: a runner calling model_fn() without the prior error leaves
        received_on_retry[0] as None → assertion fails.
        """
        from agents.fi_recall_warranty.runner import run_with_validator_fallback

        first_exc: list = [None]
        received_on_retry: list = [None]
        call_count = [0]

        def model_fn(prev_error=None):
            call_count[0] += 1
            if call_count[0] == 1:
                exc = ValueError("first-call: urgency constraint violated")
                first_exc[0] = exc
                raise exc
            else:
                received_on_retry[0] = prev_error
                raise ValueError("second-call also failed")

        run_with_validator_fallback(
            model_fn=model_fn,
            rules_fn=lambda err: {"diagnosis_source": "rules"},
            max_retries=1,
        )
        assert received_on_retry[0] is first_exc[0], (
            "The retry (second model call) must receive the first call's exception. "
            "A runner calling model_fn() with no argument leaves received_on_retry[0]=None."
        )

    def test_build_rules_plan_nearest_candidate_no_combine(self):
        """build_rules_plan(candidates, band_result) takes candidate 0 (pre-ranked by caller).

        Caller (rank_rooftops) ranks in-stock first then by distance; build_rules_plan
        always picks candidates[0]. combine_with=[], urgency from band, confidence=low,
        diagnosis_source=rules. Named function in contract § 1.6.
        """
        from agents.fi_recall_warranty import band, runner

        band_result = band.classify(
            recall_class="safety",
            do_not_drive=True,
            match_basis="CONFIRMED",
        )  # p0, urgency=this_week

        # Pre-ranked: dealer-seattle is candidate 0 (in_stock + nearest)
        candidates = [
            {"dealer_id": "dealer-seattle", "distance_miles": 8.3, "in_stock": True},
            {"dealer_id": "dealer-portland", "distance_miles": 12.5, "in_stock": True},
            {"dealer_id": "dealer-tacoma", "distance_miles": 20.1, "in_stock": False},
        ]

        plan = runner.build_rules_plan(candidates, band_result)

        assert plan["dealer_id"] == "dealer-seattle", (
            f"build_rules_plan must select candidate 0; "
            f"Got: {plan.get('dealer_id')!r}."
        )
        assert plan["combine_with"] == [], "D8 step 6: rules fallback combines nothing."
        assert plan.get("urgency") == band_result.urgency, (
            "D8 step 6: urgency must come from the band result. "
            f"band_result.urgency={band_result.urgency!r}, "
            f"plan.get('urgency')={plan.get('urgency')!r}"
        )
        assert plan.get("confidence") == "low", "Rules fallback carries confidence=low."
        assert plan.get("diagnosis_source") == "rules", (
            "Rules fallback has diagnosis_source=rules."
        )


# ============================================================================
# Item 4 — Warranty
# ============================================================================

class TestWarranty:
    """estimated_value_usd from DMS; insufficient_evidence and denied paths (contract § 2)."""

    def test_estimated_value_equals_dms_recoverable_whatever_model_returns(self):
        """estimated_value_usd must equal DMS's recoverable_usd, never model output (§ 2.7)."""
        from agents.fi_recall_warranty.warranty import build_recommended_action

        action = build_recommended_action(
            dms_recoverable_usd=850.00,
            model_value_usd=9_999.99,  # model says 10k — must be ignored
            ro_id="ro-1001",
            line_no="1",
            vin=_VIN,
            dealer_id="dealer-seattle",
            labor_op="LO-EV-BRAKE-001",
            fault_code="C0035",
            notes="Brake caliper replaced on inspection.",
        )
        assert action["estimated_value_usd"] == 850.00

    def test_estimated_value_mutation_wired_to_model_output_fails(self):
        """Mutation guard: wiring estimated_value_usd to model output makes this assert fail."""
        from agents.fi_recall_warranty.warranty import build_recommended_action

        action = build_recommended_action(
            dms_recoverable_usd=850.00,
            model_value_usd=9_999.99,
            ro_id="ro-1001",
            line_no="1",
            vin=_VIN,
            dealer_id="dealer-seattle",
            labor_op="LO-EV-BRAKE-001",
            fault_code="C0035",
            notes="Note.",
        )
        # If mutation wires to model output, action["estimated_value_usd"] == 9999.99 → fails.
        assert action["estimated_value_usd"] != 9_999.99

    def test_estimated_value_basis_carries_hours_rate_terms(self):
        """estimated_value_basis must be present and describe the computation."""
        from agents.fi_recall_warranty.warranty import build_recommended_action

        action = build_recommended_action(
            dms_recoverable_usd=850.00,
            model_value_usd=9_999.99,
            ro_id="ro-1001",
            line_no="1",
            vin=_VIN,
            dealer_id="dealer-seattle",
            labor_op="LO-EV-BRAKE-001",
            fault_code="C0035",
            notes="Note.",
        )
        assert "estimated_value_basis" in action, (
            "estimated_value_basis must be present (contract § 2.7)"
        )

    def test_insufficient_evidence_on_an_approved_verdict_has_no_recommended_action(self):
        """Security review area B W2: insufficient_evidence strips recommended_action for an
        APPROVED verdict too, so the model can never leave a fileable claim on a line it says
        lacks evidence (spec D8)."""
        from agents.fi_recall_warranty.warranty import build_finding

        finding = build_finding(
            verdict="approved",
            model_result={"proceed": "insufficient_evidence"},
            ro_id="ro-1004",
            line_no="1",
            vin=_VIN,
            dms_recoverable_usd=400.00,
        )
        assert finding is not None
        assert "recommended_action" not in finding

    def test_insufficient_evidence_produces_item_with_no_recommended_action(self):
        """insufficient_evidence on any selected line → informational item only (§ 2.2).

        Not restricted to requires_review: any verdict can return insufficient_evidence
        (spec D8).
        """
        from agents.fi_recall_warranty.warranty import build_finding

        finding = build_finding(
            verdict="requires_review",
            model_result={"proceed": "insufficient_evidence"},
            ro_id="ro-1002",
            line_no="1",
            vin=_VIN,
            dms_recoverable_usd=400.00,
        )
        assert finding is not None
        assert "recommended_action" not in finding

    def test_denied_line_produces_no_finding(self):
        """Denied lines receive no Finding at all (§ 2.2)."""
        from agents.fi_recall_warranty.warranty import build_finding

        result = build_finding(
            verdict="denied",
            model_result=None,
            ro_id="ro-1003",
            line_no="2",
            vin=_VIN,
            dms_recoverable_usd=0.00,
        )
        assert result is None


# ============================================================================
# Item 5 — Skip rule
# ============================================================================

class TestSkipRule:
    """Same-hash deduplication and per-run cap (spec: 60 recall, 30 warranty)."""

    def test_same_hash_model_written_no_call_no_write(self):
        """Same hash + diagnosis_source=model → no plan_fn call; item refreshed from live.

        Replaces the FiRwRunner-based version (brief step 4).  The property is unchanged:
        when the live row was model-written with the same inputs, the model is NOT consulted
        again — the live content is reused (refresh_from_live path).
        """
        import datetime
        from agents.fi_recall_warranty.recall import run_recall_loop

        _today = datetime.date(2026, 10, 7)
        _at = "2026-10-07T05:00:00Z"
        _row = {
            "vin": _VIN, "campaign_id": "MRD-SR-001", "recall_class": "safety",
            "do_not_drive": False, "match_basis": "POPULATION",
            "remedy_available_from": None, "remedy_part_numbers": ["PART-SR"],
            "summary": "S", "consequence": "C", "remedy": "R",
            "received_date": "2026-01-01", "catalog_version": "1", "rule_version": "1",
            "models": None, "model_year_min": None, "model_year_max": None,
            "plants": None, "build_from": None, "build_to": None,
            "park_outside": False, "confirming_code": None,
            "confirming_seen_on": None, "confirming_source": None,
        }
        _stock = [{"dealer_id": "dealer-sr", "part_number": "PART-SR",
                   "qty_on_hand": "3", "latitude": "37.33", "longitude": "-122.03",
                   "stock_as_of": _at}]

        class _F:
            def __init__(self):
                self.written: list = []
                self._kwargs: list = []
                self._live: dict = {}
                self.plan_calls: list = []
            def vehicle_ids_fn(self, vin): return ("v1", "fl1", "loc1")
            def locations_fn(self, fid): return {"loc1": (37.7, -122.4)}
            def grounded_fn(self, vid): return False, 0
            def history_fn(self, vin): return {}, 0
            def standing_fn(self, vin): return object()
            def lookup_live(self, k): return self._live.get(k)
            def write_finding(self, f, s, **kw):
                self.written.append(f)
                self._kwargs.append(kw)
                return {"action": "put"}
            def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None):
                self.plan_calls.append(item_type)
                return {"dealer_id": rooftops[0]["dealer_id"] if rooftops else None,
                        "combine_with": [], "urgency": band_result.urgency,
                        "detail": "model", "confidence": "medium",
                        "diagnosis_source": "model"}

        def _run(fakes, plan_fn=None):
            return run_recall_loop(
                recall_rows=[_row], stock_rows=_stock, today=_today, computed_at=_at,
                vehicle_ids_fn=fakes.vehicle_ids_fn, locations_fn=fakes.locations_fn,
                grounded_fn=fakes.grounded_fn, history_fn=fakes.history_fn,
                standing_fn=fakes.standing_fn, lookup_live=fakes.lookup_live,
                write_finding=fakes.write_finding, plan_fn=plan_fn,
            )

        fakes = _F()
        _run(fakes)  # first run: write the item
        assert fakes.written, "First run must write at least one finding"
        first = fakes.written[0]

        # Seed live with model-written same-hash finding.
        live = dict(first)
        live["evidence"] = [
            {"kind": "recall.diagnosis_source", "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run", "value": "model",
             "computed_at": _at, "provenance": "live"},
        ]
        fakes._live[first["finding_key"]] = live

        fakes.written.clear(); fakes._kwargs.clear(); fakes.plan_calls.clear()
        stats = _run(fakes, plan_fn=fakes.plan_fn)

        assert fakes.plan_calls == [], (
            "plan_fn must NOT be called when live row has same hash + model-written "
            f"(refresh_from_live path); got calls={fakes.plan_calls}."
        )
        assert stats["items_refreshed_from_live"] == 1, (
            f"items_refreshed_from_live must be 1; got {stats['items_refreshed_from_live']}"
        )

    def test_same_hash_rules_written_model_is_called(self):
        """Same hash + diagnosis_source=rules → eligible; plan_fn called, update_on_same_hash.

        Replaces the FiRwRunner-based version (brief step 4).  The property is unchanged:
        the item is eligible for a model call; re-writing without calling the model is wrong.
        update_on_same_hash=True is also pinned here (call 59).
        """
        import datetime
        from agents.fi_recall_warranty.recall import run_recall_loop

        _today = datetime.date(2026, 10, 7)
        _at = "2026-10-07T05:00:00Z"
        _row = {
            "vin": _VIN, "campaign_id": "MRD-SR-002", "recall_class": "safety",
            "do_not_drive": False, "match_basis": "POPULATION",
            "remedy_available_from": None, "remedy_part_numbers": ["PART-SR2"],
            "summary": "S", "consequence": "C", "remedy": "R",
            "received_date": "2026-01-01", "catalog_version": "1", "rule_version": "1",
            "models": None, "model_year_min": None, "model_year_max": None,
            "plants": None, "build_from": None, "build_to": None,
            "park_outside": False, "confirming_code": None,
            "confirming_seen_on": None, "confirming_source": None,
        }
        _stock = [{"dealer_id": "dealer-sr2", "part_number": "PART-SR2",
                   "qty_on_hand": "3", "latitude": "37.33", "longitude": "-122.03",
                   "stock_as_of": _at}]

        class _F:
            def __init__(self):
                self.written: list = []
                self._kwargs: list = []
                self._live: dict = {}
                self.plan_calls: list = []
            def vehicle_ids_fn(self, vin): return ("v1", "fl1", "loc1")
            def locations_fn(self, fid): return {"loc1": (37.7, -122.4)}
            def grounded_fn(self, vid): return False, 0
            def history_fn(self, vin): return {}, 0
            def standing_fn(self, vin): return object()
            def lookup_live(self, k): return self._live.get(k)
            def write_finding(self, f, s, **kw):
                self.written.append(f)
                self._kwargs.append(kw)
                return {"action": "put"}
            def plan_fn(self, *, row, item_type, band_result, rooftops, origin, bundle=None):
                self.plan_calls.append(item_type)
                return {"dealer_id": rooftops[0]["dealer_id"] if rooftops else None,
                        "combine_with": [], "urgency": band_result.urgency,
                        "detail": "model", "confidence": "medium",
                        "diagnosis_source": "model"}

        def _run(fakes, plan_fn=None):
            return run_recall_loop(
                recall_rows=[_row], stock_rows=_stock, today=_today, computed_at=_at,
                vehicle_ids_fn=fakes.vehicle_ids_fn, locations_fn=fakes.locations_fn,
                grounded_fn=fakes.grounded_fn, history_fn=fakes.history_fn,
                standing_fn=fakes.standing_fn, lookup_live=fakes.lookup_live,
                write_finding=fakes.write_finding, plan_fn=plan_fn,
            )

        fakes = _F()
        _run(fakes)  # first run: write the item
        assert fakes.written, "First run must write at least one finding"
        first = fakes.written[0]

        # Seed live with rules-written same-hash finding.
        live = dict(first)
        live["evidence"] = [
            {"kind": "recall.diagnosis_source", "source": "agent-fi-recall-warranty",
             "source_ref": "fi-recall-warranty-run", "value": "rules",
             "computed_at": _at, "provenance": "live"},
        ]
        fakes._live[first["finding_key"]] = live

        fakes.written.clear(); fakes._kwargs.clear(); fakes.plan_calls.clear()
        _run(fakes, plan_fn=fakes.plan_fn)

        assert len(fakes.plan_calls) == 1, (
            "The model must be called when diagnosis_source=rules (spec D8: eligible for a "
            f"model call). Got plan_calls={fakes.plan_calls}."
        )
        assert any(kw.get("update_on_same_hash") for kw in fakes._kwargs), (
            "write_finding must be called with update_on_same_hash=True when live row has "
            f"same hash + rules-written (call 59). Got write_kwargs={fakes._kwargs}"
        )

    def test_sort_by_severity_full_order(self):
        """sort_by_severity orders p0 → attention → soon → routine (all four checked)."""
        from agents.fi_recall_warranty.runner import sort_by_severity

        findings = [
            {"severity": "routine"},
            {"severity": "p0"},
            {"severity": "soon"},
            {"severity": "attention"},
        ]
        result = sort_by_severity(findings)
        severities = [f["severity"] for f in result]
        assert severities == ["p0", "attention", "soon", "routine"], (
            f"Expected full order [p0, attention, soon, routine], got {severities}. "
            "Swapping attention and soon must fail this test."
        )

    def test_apply_cap_trims_recall_to_60(self):
        """apply_cap enforces a 60-recall cap per run."""
        from agents.fi_recall_warranty.runner import apply_cap

        recalls = [
            {"finding_kind": "fleet.recall.vehicle", "severity": "routine"}
            for _ in range(70)
        ]
        result = apply_cap(recalls, recall_cap=60, warranty_cap=30)
        recall_count = sum(
            1 for r in result if r["finding_kind"] == "fleet.recall.vehicle"
        )
        assert recall_count == 60

    def test_apply_cap_trims_warranty_to_30(self):
        """apply_cap enforces a 30-warranty cap per run."""
        from agents.fi_recall_warranty.runner import apply_cap

        warranties = [
            {"finding_kind": "fleet.warranty.claim_candidate", "severity": "soon"}
            for _ in range(40)
        ]
        result = apply_cap(warranties, recall_cap=60, warranty_cap=30)
        warranty_count = sum(
            1 for r in result if r["finding_kind"] == "fleet.warranty.claim_candidate"
        )
        assert warranty_count == 30

    def test_apply_cap_uses_highest_severity_first_within_type(self):
        """Highest-severity items survive the cap; lower-severity ones are dropped."""
        from agents.fi_recall_warranty.runner import apply_cap

        # 3 p0 recalls + 2 routine recalls; cap at 3.
        recalls = (
            [{"finding_kind": "fleet.recall.vehicle", "severity": "p0"}] * 3
            + [{"finding_kind": "fleet.recall.vehicle", "severity": "routine"}] * 2
        )
        result = apply_cap(recalls, recall_cap=3, warranty_cap=30)
        surviving = [r for r in result if r["finding_kind"] == "fleet.recall.vehicle"]
        assert len(surviving) == 3
        assert all(r["severity"] == "p0" for r in surviving)


# ============================================================================
# Item 6 — Attempts (D12 / contract § 4)
# ============================================================================

class TestAttemptRules:
    """D12 attempt rules: which terminal decisions trigger a next attempt and when."""

    def test_cancelled_triggers_next_attempt_immediately(self):
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="cancelled", recall_class="safety", days_since_decision=0
        ) is True

    def test_failed_triggers_next_attempt_immediately(self):
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="failed", recall_class="non_safety", days_since_decision=0
        ) is True

    def test_refused_triggers_next_attempt_immediately(self):
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="refused", recall_class="safety", days_since_decision=0
        ) is True

    def test_rejected_safety_not_yet_14_days_no_retry(self):
        """Declined (rejected) safety recall: no retry before 14 days (D12)."""
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="rejected", recall_class="safety", days_since_decision=13
        ) is False

    def test_rejected_safety_exactly_14_days_retries(self):
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="rejected", recall_class="safety", days_since_decision=14
        ) is True

    def test_rejected_non_safety_not_yet_30_days_no_retry(self):
        """Declined (rejected) non-safety recall: no retry before 30 days (D12)."""
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="rejected", recall_class="non_safety", days_since_decision=29
        ) is False

    def test_rejected_non_safety_exactly_30_days_retries(self):
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="rejected", recall_class="non_safety", days_since_decision=30
        ) is True

    def test_stalled_never_retries(self):
        """A stalled attempt produces no retry — informational escalation instead (D12)."""
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="stalled", recall_class="safety", days_since_decision=365
        ) is False

    def test_stalled_also_not_retried_for_non_safety(self):
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="stalled", recall_class="non_safety", days_since_decision=365
        ) is False

    def test_executed_does_not_retry(self):
        """executed → False: vehicle already remedied or grounded (contract § 4, table).

        N9 boundary: a should_retry that falls through to True for statuses not explicitly
        handled (including executed) makes this return True → fails this assertion.
        """
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="executed", recall_class="safety", days_since_decision=0
        ) is False, (
            "executed must not trigger a retry (vehicle is remedied or grounded). "
            "N9 check: a fall-through to True makes this return True."
        )

    def test_completed_does_not_retry(self):
        """completed → False: vehicle remedied (contract § 4, table).

        N9 boundary: same fall-through mutant as for executed.
        """
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="completed", recall_class="non_safety", days_since_decision=0
        ) is False, (
            "completed must not trigger a retry (vehicle is remedied). "
            "N9 check: a fall-through to True makes this return True."
        )

    def test_blocked_does_not_retry(self):
        """blocked → False: guardrail block, no automatic retry (contract § 4, table).

        N9 boundary: same fall-through mutant as for executed.
        """
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="blocked", recall_class="safety", days_since_decision=0
        ) is False, (
            "blocked must not trigger a retry (guardrail block, inform operator instead). "
            "N9 check: a fall-through to True makes this return True."
        )

    def test_sixth_attempt_is_not_written(self):
        """After 5 attempts (max cap), attempt 6 must not be written.

        The cap is 5 attempts per (vehicle, campaign, action). After that only an
        informational item is written; no further attempt.
        """
        from agents.fi_recall_warranty.attempts import should_retry

        # attempt_number 5 means we're considering attempt 6 — must be False.
        assert should_retry(
            terminal_decision="cancelled",
            recall_class="safety",
            days_since_decision=0,
            attempt_number=5,  # 5th attempt just resolved; no 6th
        ) is False, (
            "Attempt 6 must not be written (max 5 per D12). "
            "should_retry with attempt_number=5 must return False."
        )

    def test_fifth_attempt_is_still_written(self):
        """attempt_number=4 → True: the 5th attempt can still be written.

        N4 boundary: a cap of 4 (not 5) makes should_retry(attempt_number=4) return
        False, blocking the 5th attempt. The cap must be 5.
        """
        from agents.fi_recall_warranty.attempts import should_retry

        assert should_retry(
            terminal_decision="cancelled",
            recall_class="safety",
            days_since_decision=0,
            attempt_number=4,  # 4th attempt just resolved; 5th is still allowed
        ) is True, (
            "Attempt 5 must be written (cap is 5, not 4). "
            "should_retry with attempt_number=4 must return True. "
            "N4 check: a cap of 4 makes this return False."
        )

    def test_attempt_n_plus_1_uses_new_subject_key(self):
        """Attempt n+1 has subject_key <id>/<action>/<n+1>, a new Finding.

        This is the core of D12: if the key is reused, the 'decision is final for
        as long as the Finding exists' rule means the recall never comes back.
        """
        from agents.fi_recall_warranty.attempts import build_next_subject_key

        sk = build_next_subject_key(
            campaign_id="MRD-2024-001",
            action="book",
            current_attempt=1,
        )
        assert sk == "MRD-2024-001/book/2", (
            f"Attempt 2's subject_key must be MRD-2024-001/book/2, got {sk!r}. "
            "Re-using the current attempt's key would prevent the recall from re-appearing."
        )

    def test_after_5_attempts_informational_item_has_no_recommended_action(self):
        """After 5 failed attempts the informational item carries no recommended_action (D12)."""
        from agents.fi_recall_warranty.attempts import build_max_attempts_item

        item = build_max_attempts_item(
            vin=_VIN,
            campaign_id="MRD-2024-001",
            action="book",
        )
        assert item is not None
        assert "recommended_action" not in item

    def test_after_5_attempts_informational_headline_mentions_manual_followup(self):
        """The informational item's headline equals the fixed template exactly (contract § 1.4).

        N5 boundary: a headline of 'Manual review suggested' contains 'manual' but is not
        the fixed template. The test asserts exact equality so N5 fails.
        Headline must be exactly: 'Bookings keep failing — manual follow-up needed.'
        (with the period; see decisions.md call 12 and contract § 1.4).
        """
        from agents.fi_recall_warranty.attempts import build_max_attempts_item

        item = build_max_attempts_item(
            vin=_VIN,
            campaign_id="MRD-2024-001",
            action="book",
        )
        headline = item.get("headline", "")
        assert headline == "Bookings keep failing — manual follow-up needed.", (
            f"Informational headline must equal the fixed template exactly. "
            f"Got {headline!r}. "
            "N5 check: 'Manual review suggested' contains 'manual' but is not the template."
        )


# ============================================================================
# Item 7 — Athena NextToken
# ============================================================================

# TestAthenaPagination was removed with runner.paginate_athena_results (Fix Group G3-1 F4b,
# review cycle 1 W6): production pages in agents.tier2.adp_source.run_query, driven through a
# botocore Stubber with three GetQueryResults pages in
# tests/test_selection_fix_f3.py::TestSelectRecallsThreePages.
