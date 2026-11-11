"""Tests for run_warranty_loop and build_warranty_evidence — task 3.1H.

Coverage
--------
- approved + requires_review rows → two writes.
- denied row → no write (mutation guard a).
- estimated_value_usd equals the row's recoverable_usd (mutation guard b).
- line_no is a string in parameters even when the row provides an integer.
- every written Finding validates against agents/tier2/schemas/finding.schema.json.
- second identical run with model-written live rows → zero writes.
- bad row (missing vehicle_vin) skipped; remaining row still written.
- standing passed verbatim to write_finding (mutation guard c).
- narrative truncated to 1000 chars (mutation guard d).

Mutation anchors
----------------
(a) denied check removed → test_denied_produces_no_write
(b) row recoverable_usd replaced by plan value → test_estimated_value_equals_recoverable_usd
(c) standing omitted (None passed) → test_standing_passed_verbatim
(d) narrative truncation removed → test_narrative_truncated_to_1000
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

# ---------------------------------------------------------------------------
# Schema validator
# ---------------------------------------------------------------------------

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2] / "tier2" / "schemas" / "finding.schema.json"
    ).read_text()
)


def _validate(finding: dict) -> None:
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(_SCHEMA)
    errors = list(validator.iter_errors(finding))
    assert not errors, (
        f"Finding failed schema validation ({len(errors)} error(s)): "
        + "; ".join(e.message for e in errors[:3])
    )


# ---------------------------------------------------------------------------
# Fixtures — minimal warranty candidate row
# ---------------------------------------------------------------------------

_COMPUTED_AT = "2026-10-07T05:00:00Z"
_VIN_A = "MRDVEH00000000100"
_VIN_B = "MRDVEH00000000101"


def _base_row(
    *,
    vin: str = _VIN_A,
    ro_id: str = "RO-001",
    line_no: str = "3",
    verdict_status: str = "approved",
    recoverable_usd: str = "450.00",
    labor_op: str = "LO-BRAKE-001",
    dealer_id: str = "dealer-dms-01",
    fault_code: str = "C0035",
    coverage_group: str = "Powertrain",
    in_service_date: str = "2023-01-15",
    odometer_km: str = "45000",
    repair_date: str = "2026-09-01",
    verdict_reason: str = "Within mileage term",
    matched_rule_id: str = "RULE-101",
    terms_version: str = "v1.2",
    filing_deadline: str = "2026-11-01T00:00:00Z",
) -> dict:
    return {
        "vehicle_vin": vin,
        "ro_id": ro_id,
        "line_no": line_no,
        "verdict_status": verdict_status,
        "recoverable_usd": recoverable_usd,
        "labor_op": labor_op,
        "dealer_id": dealer_id,
        "fault_code": fault_code,
        "coverage_group": coverage_group,
        "in_service_date": in_service_date,
        "odometer_km": odometer_km,
        "repair_date": repair_date,
        "verdict_reason": verdict_reason,
        "matched_rule_id": matched_rule_id,
        "terms_version": terms_version,
        "filing_deadline": filing_deadline,
    }


# ---------------------------------------------------------------------------
# Recording fake
# ---------------------------------------------------------------------------

class _StandingFake:
    def __init__(self, vin: str):
        self.vin = vin


class _Fakes:
    def __init__(self):
        self.written: list[tuple[dict, Any]] = []
        self._live: dict[str, dict] = {}

    def standing_fn(self, vin: str) -> _StandingFake:
        return _StandingFake(vin)

    def lookup_live(self, finding_key: str) -> Optional[dict]:
        return self._live.get(finding_key)

    def write_finding(self, finding: dict, standing: Any, refresh_on_noop: bool = False) -> dict:
        self.written.append((finding, standing))
        return {"action": "put"}


#: Current DMS terms for the fixture rows' coverage group (F4: rows without terms are refused).
_TERMS = {"Powertrain": {"terms_version": "1.0.0", "coverage_group": "Powertrain",
                         "effective_from": "2024-01-01", "term_years": 5, "term_miles": 60_000,
                         "labor_rate_usd": 140.0}}


def _run(rows: list[dict], fakes: _Fakes, **kwargs) -> dict:
    from agents.fi_recall_warranty.warranty import run_warranty_loop
    kwargs.setdefault("warranty_terms", _TERMS)
    return run_warranty_loop(
        candidate_rows=rows,
        computed_at=_COMPUTED_AT,
        standing_fn=fakes.standing_fn,
        lookup_live=fakes.lookup_live,
        write_finding=fakes.write_finding,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test: approved + requires_review produce two writes
# ---------------------------------------------------------------------------

class TestApprovedAndRequiresReview:
    def test_two_eligible_rows_produce_two_writes(self):
        rows = [
            _base_row(vin=_VIN_A, ro_id="RO-001", line_no="1", verdict_status="approved"),
            _base_row(vin=_VIN_B, ro_id="RO-002", line_no="2",
                      verdict_status="requires_review"),
        ]
        fakes = _Fakes()
        stats = _run(rows, fakes)
        assert stats["items_written"] == 2, (
            f"approved + requires_review must produce 2 writes; got {stats['items_written']}"
        )
        assert len(fakes.written) == 2, (
            f"write_finding must be called twice; called {len(fakes.written)} times"
        )


# ---------------------------------------------------------------------------
# Test: denied produces no write  (mutation guard a)
# ---------------------------------------------------------------------------

class TestDeniedProducesNoWrite:
    def test_denied_produces_no_write(self):
        """Mutation (a): removing the denied check causes denied row to be written,
        making len(fakes.written) == 1 instead of 0."""
        row = _base_row(verdict_status="denied")
        fakes = _Fakes()
        stats = _run([row], fakes)
        assert len(fakes.written) == 0, (
            "Mutation (a): denied lines written — "
            f"write_finding must not be called for denied; called {len(fakes.written)} times"
        )
        assert stats["rows_refused"] >= 1, "denied row must increment rows_refused"


# ---------------------------------------------------------------------------
# Test: estimated_value_usd equals the row's recoverable_usd  (mutation guard b)
# ---------------------------------------------------------------------------

class TestEstimatedValueEqualsRecoverableUsd:
    def test_estimated_value_equals_recoverable_usd(self):
        """Mutation (b): using plan value instead of the row causes
        estimated_value_usd != 450.0, failing this assertion."""
        row = _base_row(recoverable_usd="450.00")
        fakes = _Fakes()
        _run([row], fakes)
        assert fakes.written, "Expected at least one write"
        finding, _ = fakes.written[0]
        action = finding.get("recommended_action", {})
        usd = action.get("estimated_value_usd")
        assert usd == 450.0, (
            "Mutation (b): value from plan instead of row — "
            f"estimated_value_usd must be 450.0 (from row); got {usd!r}"
        )


# ---------------------------------------------------------------------------
# Test: line_no is a string in parameters for an int-valued row
# ---------------------------------------------------------------------------

class TestLineNoIsString:
    def test_int_line_no_is_stringified_in_parameters(self):
        """G3-6: integers avoid DynamoDB Decimal round-trips; run_warranty_loop
        must stringify the row's int line_no when passing to build_finding."""
        row = _base_row(line_no=3)   # type: ignore[arg-type]  int, not str
        fakes = _Fakes()
        _run([row], fakes)
        assert fakes.written, "Expected at least one write"
        finding, _ = fakes.written[0]
        params = finding.get("recommended_action", {}).get("parameters", {})
        assert isinstance(params.get("line_no"), str), (
            f"line_no in parameters must be a string even for int input; "
            f"got {type(params.get('line_no'))!r}"
        )


# ---------------------------------------------------------------------------
# Test: every Finding validates against finding.schema.json
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_all_written_findings_validate(self):
        rows = [
            _base_row(vin=_VIN_A, verdict_status="approved"),
            _base_row(vin=_VIN_B, verdict_status="requires_review"),
        ]
        fakes = _Fakes()
        _run(rows, fakes)
        assert fakes.written, "Expected at least one write"
        for finding, _ in fakes.written:
            _validate(finding)


# ---------------------------------------------------------------------------
# Test: second identical run with model-written live rows → zero writes
# ---------------------------------------------------------------------------

class TestModelWrittenSkip:
    def test_identical_run_model_written_skips(self):
        """CHANGED by FI stage C Group 4 (S3a step 4, architect-authorized): second run with
        same inputs and model-written live rows now writes exactly one refresh per item from
        the live row's model content with refresh_on_noop=True.  The pre-change contract
        ("zero writes") let an unchanged model-written item lapse after its 3-day TTL."""
        rows = [_base_row()]
        fakes = _Fakes()
        _run(rows, fakes)
        assert fakes.written, "First run must write at least one finding"

        # Seed the live store with the written finding, marking it model-written.
        for finding, _ in fakes.written:
            live = dict(finding)
            # Replace evidence with a model-written marker the skip rule reads.
            live["evidence"] = [
                {"kind": "warranty.diagnosis_source", "value": "model",
                 "source": "agent-fi-recall-warranty",
                 "source_ref": "fi-recall-warranty-run",
                 "computed_at": _COMPUTED_AT, "provenance": "live"},
            ]
            fakes._live[finding["finding_key"]] = live

        # Second run: one refresh write per item (refresh_from_live, not a new write).
        fakes2 = _Fakes()
        fakes2._live = fakes._live
        fakes2.standing_fn = fakes.standing_fn
        stats2 = _run(rows, fakes2)
        assert len(fakes2.written) == len(fakes.written), (
            f"Second run must write one refresh per model-written item; "
            f"wrote {len(fakes2.written)} (expected {len(fakes.written)})"
        )
        assert stats2["items_skipped_unchanged"] >= 1, "items_skipped_unchanged must be incremented"
        assert stats2["items_refreshed_from_live"] >= 1, "items_refreshed_from_live must be incremented"


# ---------------------------------------------------------------------------
# Test: bad row skipped, rest written
# ---------------------------------------------------------------------------

class TestBadRowSkipped:
    def test_bad_row_skipped_rest_written(self):
        """A bad row (missing vehicle_vin) must be skipped; the next row must write.

        Mutation (a) partial: aborting on a bad row would mean items_written=0.
        """
        bad = {"ro_id": "RO-BAD", "line_no": "1", "verdict_status": "approved",
               "recoverable_usd": "bad-value"}  # float() will fail
        good = _base_row(vin=_VIN_A, ro_id="RO-GOOD")
        fakes = _Fakes()
        stats = _run([bad, good], fakes)
        assert stats["rows_refused"] >= 1, (
            "Bad row must increment rows_refused; run must not abort"
        )
        assert stats["items_written"] >= 1, (
            "Good row after bad must still be written; "
            "bad row skipped, rest written"
        )
        assert any(f["scope"]["vin"] == _VIN_A for (f, _) in fakes.written), (
            "Good row VIN must appear in written findings"
        )


# ---------------------------------------------------------------------------
# Test: standing passed verbatim to write_finding  (mutation guard c)
# ---------------------------------------------------------------------------

class TestStandingPassedVerbatim:
    def test_standing_passed_verbatim(self):
        """Mutation (c): omitting standing (passing None) causes the second element
        of the write_finding call to be None instead of the StandingFake,
        failing the is-not-None assertion below."""
        row = _base_row()
        fakes = _Fakes()
        _run([row], fakes)
        assert fakes.written, "Expected at least one write"
        _, standing = fakes.written[0]
        assert standing is not None, (
            "Mutation (c): standing omitted — "
            "write_finding must receive the standing object from standing_fn, not None"
        )
        assert hasattr(standing, "vin"), (
            "standing must be the StandingFake from standing_fn (has .vin attribute)"
        )
        assert standing.vin == _VIN_A, (
            f"standing.vin must match the row VIN {_VIN_A!r}; got {standing.vin!r}"
        )


# ---------------------------------------------------------------------------
# Test: narrative truncated to 1000 chars  (mutation guard d)
# ---------------------------------------------------------------------------

class TestNarrativeTruncated:
    def test_narrative_truncated_to_1000(self):
        """Mutation (d): removing [:1_000] from _rules_narrative lets a long
        narrative through, making the len assertion below fail.

        Tested via _rules_narrative directly because build_recommended_action
        also clips notes to 1_000, so the loop-level notes parameter alone
        cannot distinguish the two: we must catch the truncation at the source.
        """
        from agents.fi_recall_warranty.warranty import _rules_narrative
        row = _base_row(labor_op="X" * 950, verdict_reason="Y" * 100)
        narrative = _rules_narrative(row)
        assert len(narrative) <= 1000, (
            "Mutation (d): narrative longer than 1000 accepted — "
            f"_rules_narrative must return at most 1000 chars; got {len(narrative)}"
        )
