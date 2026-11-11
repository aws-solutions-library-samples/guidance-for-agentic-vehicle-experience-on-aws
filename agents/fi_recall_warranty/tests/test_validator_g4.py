"""Both-sides tests for validator.py (task 4.1a, spec D8/D9, decisions.md calls 48–49).

Every rule is tested twice: the valid side (accepted, no raise) and the
violation side (raises ValidationError).
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valid_recall_input(**overrides) -> dict:
    base = {
        "dealer_id": "dealer-seattle",
        "combine_with": [],
        "urgency": "this_week",
        "detail": "Book the recall service at the nearest available dealer.",
        "evidence_refs": ["recall.data"],
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _valid_claim_input(**overrides) -> dict:
    base = {
        "narrative": "Warranty claim for powertrain failure.",
        "proceed": "file",
        "detail": "Submit claim with supporting documentation.",
        "evidence_refs": ["warranty.data"],
        "confidence": "medium",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# RecallValidator — dealer
# ---------------------------------------------------------------------------
class TestRecallValidatorDealer:
    def test_dealer_in_candidates_accepted(self):
        from agents.fi_recall_warranty.validator import RecallValidator
        v = RecallValidator(candidate_dealer_ids=["dealer-seattle"])
        v.validate(dealer_id="dealer-seattle", combine_with=None, urgency=None)

    def test_dealer_outside_candidates_rejected(self):
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError
        v = RecallValidator(candidate_dealer_ids=["dealer-seattle"])
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-denver", combine_with=None, urgency=None)

    def test_empty_candidates_rejects_any_dealer(self):
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError
        v = RecallValidator(candidate_dealer_ids=[])
        with pytest.raises(ValidationError, match="candidate"):
            v.validate(dealer_id="dealer-seattle", combine_with=None, urgency=None)


# ---------------------------------------------------------------------------
# RecallValidator — combine_with
# ---------------------------------------------------------------------------
class TestRecallValidatorCombineWith:
    def test_combine_with_inside_bundle_accepted(self):
        from agents.fi_recall_warranty.validator import RecallValidator
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=["MRD-2024-001"],
        )
        v.validate(dealer_id="dealer-seattle", combine_with=["MRD-2024-001"], urgency=None)

    def test_combine_with_none_accepted(self):
        from agents.fi_recall_warranty.validator import RecallValidator
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=["MRD-2024-001"],
        )
        v.validate(dealer_id="dealer-seattle", combine_with=None, urgency=None)

    def test_combine_with_outside_bundle_rejected(self):
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

    def test_empty_bundle_rejects_nonempty_combine_with(self):
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=[],
        )
        with pytest.raises(ValidationError, match="combine_with"):
            v.validate(
                dealer_id="dealer-seattle",
                combine_with=["AVX-FND-invented"],
                urgency=None,
            )


# ---------------------------------------------------------------------------
# RecallValidator — urgency: p0 non_safety
# ---------------------------------------------------------------------------
class TestRecallValidatorUrgencyP0NonSafety:
    """p0 overrides non_safety: only this_week is allowed regardless of class."""

    def test_p0_non_safety_this_week_accepted(self):
        from agents.fi_recall_warranty.validator import RecallValidator
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["p0"],
            recall_class="non_safety",
        )
        v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="this_week")

    def test_p0_non_safety_next_visit_rejected(self):
        """p0 check fires first regardless of recall_class."""
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["p0"],
            recall_class="non_safety",
        )
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="next_visit")

    def test_p0_non_safety_this_month_rejected(self):
        from agents.fi_recall_warranty.validator import RecallValidator, ValidationError
        v = RecallValidator(
            candidate_dealer_ids=["dealer-seattle"],
            allowed_severities=["p0"],
            recall_class="non_safety",
        )
        with pytest.raises(ValidationError):
            v.validate(dealer_id="dealer-seattle", combine_with=None, urgency="this_month")


# ---------------------------------------------------------------------------
# validate_recall_plan — detail length 600/601
# ---------------------------------------------------------------------------
class TestRecallPlanDetailLength:
    def test_detail_600_chars_accepted(self):
        from agents.fi_recall_warranty.validator import validate_recall_plan
        inp = _valid_recall_input(detail="x" * 600)
        validate_recall_plan(
            inp,
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=[],
            severity="soon",
            recall_class="non_safety",
            evidence_kinds=["recall.data"],
        )

    def test_detail_601_chars_rejected(self):
        from agents.fi_recall_warranty.validator import validate_recall_plan, ValidationError
        inp = _valid_recall_input(detail="x" * 601)
        with pytest.raises(ValidationError):
            validate_recall_plan(
                inp,
                candidate_dealer_ids=["dealer-seattle"],
                bundle_ids=[],
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=["recall.data"],
            )


# ---------------------------------------------------------------------------
# validate_claim_draft — narrative length 1000/1001
# ---------------------------------------------------------------------------
class TestClaimDraftNarrativeLength:
    def test_narrative_1000_chars_accepted(self):
        from agents.fi_recall_warranty.validator import validate_claim_draft
        inp = _valid_claim_input(narrative="n" * 1000)
        validate_claim_draft(inp, evidence_kinds=["warranty.data"])

    def test_narrative_1001_chars_rejected(self):
        from agents.fi_recall_warranty.validator import validate_claim_draft, ValidationError
        inp = _valid_claim_input(narrative="n" * 1001)
        with pytest.raises(ValidationError):
            validate_claim_draft(inp, evidence_kinds=["warranty.data"])


# ---------------------------------------------------------------------------
# validate_recall_plan — extra keys rejected
# ---------------------------------------------------------------------------
class TestRecallPlanExtraKeys:
    def _call(self, inp):
        from agents.fi_recall_warranty.validator import validate_recall_plan
        validate_recall_plan(
            inp,
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=[],
            severity="soon",
            recall_class="non_safety",
            evidence_kinds=["recall.data"],
        )

    def test_valid_input_accepted(self):
        self._call(_valid_recall_input())

    def test_extra_key_severity_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_recall_input(severity="p0"))

    def test_extra_key_headline_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_recall_input(headline="Some headline"))

    def test_extra_key_verdict_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_recall_input(verdict="approved"))

    def test_extra_key_estimated_value_usd_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_recall_input(estimated_value_usd=500.0))


# ---------------------------------------------------------------------------
# validate_claim_draft — extra keys rejected
# ---------------------------------------------------------------------------
class TestClaimDraftExtraKeys:
    def _call(self, inp):
        from agents.fi_recall_warranty.validator import validate_claim_draft
        validate_claim_draft(inp, evidence_kinds=["warranty.data"])

    def test_valid_input_accepted(self):
        self._call(_valid_claim_input())

    def test_extra_key_severity_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_claim_input(severity="attention"))

    def test_extra_key_headline_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_claim_input(headline="Claim headline"))

    def test_extra_key_verdict_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_claim_input(verdict="denied"))

    def test_extra_key_estimated_value_usd_rejected(self):
        from agents.fi_recall_warranty.validator import ValidationError
        with pytest.raises(ValidationError):
            self._call(_valid_claim_input(estimated_value_usd=1200.0))


# ---------------------------------------------------------------------------
# evidence_refs — unknown kind rejected
# ---------------------------------------------------------------------------
class TestEvidenceRefsKind:
    def test_known_evidence_kind_accepted(self):
        from agents.fi_recall_warranty.validator import validate_recall_plan
        inp = _valid_recall_input(evidence_refs=["recall.data"])
        validate_recall_plan(
            inp,
            candidate_dealer_ids=["dealer-seattle"],
            bundle_ids=[],
            severity="soon",
            recall_class="non_safety",
            evidence_kinds=["recall.data", "recall.history"],
        )

    def test_unknown_evidence_kind_rejected(self):
        from agents.fi_recall_warranty.validator import validate_recall_plan, ValidationError
        inp = _valid_recall_input(evidence_refs=["unknown.kind"])
        with pytest.raises(ValidationError):
            validate_recall_plan(
                inp,
                candidate_dealer_ids=["dealer-seattle"],
                bundle_ids=[],
                severity="soon",
                recall_class="non_safety",
                evidence_kinds=["recall.data"],
            )

    def test_unknown_claim_evidence_kind_rejected(self):
        from agents.fi_recall_warranty.validator import validate_claim_draft, ValidationError
        inp = _valid_claim_input(evidence_refs=["unknown.kind"])
        with pytest.raises(ValidationError):
            validate_claim_draft(inp, evidence_kinds=["warranty.data"])
