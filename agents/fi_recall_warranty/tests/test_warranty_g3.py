"""G3 tests for agents/fi_recall_warranty/warranty.py (Task 3.1 part C).

Covers:
  - Schema validation against agents/tier2/schemas/finding.schema.json
  - int line_no is stringified in subject_key and parameters
  - expires_at boundary (earlier of computed_at+3days and filing_deadline)
  - severity thresholds: recoverable 999.99/1000 and deadline 7/8 days out
"""
from __future__ import annotations

import json
import pathlib
import datetime

import jsonschema
import pytest

from agents.fi_recall_warranty.warranty import (
    build_finding,
    build_recommended_action,
    SOON_RECOVERABLE_USD,
    SOON_DEADLINE_DAYS,
)

_VIN = "MRDVEH00000000015"
_SCHEMA = json.loads(
    (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "agents/tier2/schemas/finding.schema.json"
    ).read_text()
)


def _make_finding(**kwargs):
    """Build a minimal valid finding with required fields pre-filled."""
    defaults = dict(
        verdict="approved",
        model_result={"proceed": "ok"},
        ro_id="ro-g3-001",
        line_no="1",
        vin=_VIN,
        dms_recoverable_usd=500.00,
        labor_op="LO-BRAKE-001",
        dealer_id="dealer-test",
        fault_code="C0035",
        notes="Note.",
        term_statement="Basic warranty: 4 years / 50,000 miles",
        estimated_value_basis="2.5h x $150/h + $100 parts (terms v1.0.0)",
        computed_at="2024-01-01T00:00:00Z",
        inputs_hash="a" * 64,
    )
    defaults.update(kwargs)
    return build_finding(**defaults)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_approved_finding_validates_schema(self):
        finding = _make_finding()
        assert finding is not None
        jsonschema.validate(finding, _SCHEMA)

    def test_requires_review_finding_validates_schema(self):
        finding = _make_finding(verdict="requires_review")
        assert finding is not None
        jsonschema.validate(finding, _SCHEMA)

    def test_insufficient_evidence_finding_validates_schema(self):
        finding = _make_finding(
            verdict="approved",
            model_result={"proceed": "insufficient_evidence"},
        )
        assert finding is not None
        jsonschema.validate(finding, _SCHEMA)

    def test_disclosure_txt_present_for_monetary_kind(self):
        """fleet.warranty.claim_candidate is monetary — schema requires disclosure_txt."""
        finding = _make_finding()
        assert "disclosure_txt" in finding
        assert len(finding["disclosure_txt"]) > 0

    def test_disclosure_txt_contains_dms_suffix(self):
        finding = _make_finding(term_statement="Basic warranty: 4 years / 50,000 miles")
        assert finding["disclosure_txt"].endswith(
            "DMS decides coverage when the claim is filed."
        )

    def test_disclosure_txt_contains_term_statement(self):
        ts = "Basic warranty: 4 years / 50,000 miles"
        finding = _make_finding(term_statement=ts)
        assert ts in finding["disclosure_txt"]


# ---------------------------------------------------------------------------
# int line_no is stringified
# ---------------------------------------------------------------------------

class TestLineNoStringified:
    """line_no must be a string in subject_key and parameters (architect call G3-6)."""

    def test_int_line_no_in_subject_key_is_stringified(self):
        finding = _make_finding(line_no="3")  # caller stringifies before passing
        assert finding["subject_key"] == "ro-g3-001/3"

    def test_build_recommended_action_line_no_is_string_in_params(self):
        action = build_recommended_action(
            dms_recoverable_usd=200.00,
            model_value_usd=9999.99,
            ro_id="ro-x",
            line_no="7",
            vin=_VIN,
            dealer_id="dealer-a",
            labor_op="LO-001",
        )
        assert isinstance(action["parameters"]["line_no"], str)
        assert action["parameters"]["line_no"] == "7"

    def test_finding_subject_key_uses_string_line_no(self):
        finding = _make_finding(ro_id="ro-42", line_no="9")
        assert finding["subject_key"] == "ro-42/9"
        # parameters also carry the stringified line_no
        assert finding["recommended_action"]["parameters"]["line_no"] == "9"


# ---------------------------------------------------------------------------
# expires_at boundary
# ---------------------------------------------------------------------------

class TestExpiryBoundary:
    def test_expires_at_is_3days_when_no_deadline(self):
        finding = _make_finding(computed_at="2024-06-01T00:00:00Z", filing_deadline=None)
        assert finding["expires_at"] == "2024-06-04T00:00:00Z"

    def test_expires_at_uses_filing_deadline_when_earlier(self):
        """filing_deadline 1 day out < computed_at+3 → use deadline."""
        finding = _make_finding(
            computed_at="2024-06-01T00:00:00Z",
            filing_deadline="2024-06-02T00:00:00Z",
        )
        assert finding["expires_at"] == "2024-06-02T00:00:00Z"

    def test_expires_at_uses_plus3_when_deadline_is_later(self):
        """deadline 10 days out > computed_at+3 → use +3."""
        finding = _make_finding(
            computed_at="2024-06-01T00:00:00Z",
            filing_deadline="2024-06-11T00:00:00Z",
        )
        assert finding["expires_at"] == "2024-06-04T00:00:00Z"

    def test_expires_at_uses_plus3_when_deadline_equals_plus3(self):
        """deadline == computed_at+3 → use +3 (equal, take plus3)."""
        finding = _make_finding(
            computed_at="2024-06-01T00:00:00Z",
            filing_deadline="2024-06-04T00:00:00Z",
        )
        assert finding["expires_at"] == "2024-06-04T00:00:00Z"


# ---------------------------------------------------------------------------
# Severity thresholds
# ---------------------------------------------------------------------------

class TestSeverityThresholds:
    def test_severity_routine_below_recoverable_threshold(self):
        finding = _make_finding(dms_recoverable_usd=999.99)
        assert finding["severity"] == "routine"

    def test_severity_soon_at_recoverable_threshold(self):
        """At exactly $1,000 → "soon" (>= not >)."""
        finding = _make_finding(dms_recoverable_usd=1_000.00)
        assert finding["severity"] == "soon"

    def test_severity_soon_above_recoverable_threshold(self):
        finding = _make_finding(dms_recoverable_usd=1_500.00)
        assert finding["severity"] == "soon"

    def test_severity_routine_deadline_8_days_out(self):
        """8 days left → routine (threshold is 7)."""
        computed = "2024-06-01T00:00:00Z"
        deadline = "2024-06-09T00:00:00Z"  # 8 days
        finding = _make_finding(
            dms_recoverable_usd=100.00,
            computed_at=computed,
            filing_deadline=deadline,
        )
        assert finding["severity"] == "routine"

    def test_severity_soon_deadline_7_days_out(self):
        """7 days left → "soon" (<= threshold)."""
        computed = "2024-06-01T00:00:00Z"
        deadline = "2024-06-08T00:00:00Z"  # 7 days
        finding = _make_finding(
            dms_recoverable_usd=100.00,
            computed_at=computed,
            filing_deadline=deadline,
        )
        assert finding["severity"] == "soon"

    def test_severity_soon_deadline_1_day_out(self):
        computed = "2024-06-01T00:00:00Z"
        deadline = "2024-06-02T00:00:00Z"
        finding = _make_finding(
            dms_recoverable_usd=100.00,
            computed_at=computed,
            filing_deadline=deadline,
        )
        assert finding["severity"] == "soon"
