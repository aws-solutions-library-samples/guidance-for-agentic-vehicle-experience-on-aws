"""F4a tests: W4 (date-only deadline) and W5 (warranty_terms, km→miles, semver).

DMS-shape rows: date-only filing_deadline, integer line_no, km odometer.
All assertions check values, not key presence.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from agents.fi_recall_warranty.warranty import (
    _KM_TO_MILES,
    _parse_deadline,
    build_warranty_evidence,
    run_warranty_loop,
)
from agents.fi_recall_warranty.selection import select_warranty_terms

_VIN = "MRDVEH00000000099"
# noon UTC so +3 days = "2026-10-12T12:00:00Z"
_COMPUTED_AT = "2026-10-09T12:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _terms_row(
    coverage_group: str = "basic",
    term_years: int = 4,
    term_miles: int = 50_000,
    labor_rate_usd: float = 150.0,
    terms_version: str = "1.0.0",
) -> dict:
    return {
        "terms_version": terms_version,
        "coverage_group": coverage_group,
        "term_years": term_years,
        "term_miles": term_miles,
        "labor_rate_usd": labor_rate_usd,
        "effective_from": "2024-01-01",
    }


def _row(**overrides) -> dict:
    """DMS-shape candidate row: date-only deadline, integer line_no, km odometer."""
    base: dict[str, Any] = {
        "vehicle_vin": _VIN,
        "ro_id": "RO-F4A-001",
        "line_no": 3,                    # integer — DMS DDB native
        "verdict_status": "approved",
        "labor_op": "LO-BRAKE-001",
        "fault_code": "C0035",
        "coverage_group": "basic",
        "dealer_id": "dealer-dms-01",
        "recoverable_usd": "500.00",
        "terms_version": "1.0.0",
        "in_service_date": "2023-01-15",
        "repair_date": "2026-09-01",
        "odometer_km": 45_000,           # integer km — DMS DDB native
        "verdict_reason": "Within mileage term",
        "matched_rule_id": "RULE-101",
        "filing_deadline": "2026-10-11", # date-only — DMS DDB native (2 days out)
    }
    base.update(overrides)
    return base


class _Fakes:
    def __init__(self):
        self.written: list[dict] = []

    def standing_fn(self, vin: str):
        return None

    def lookup_live(self, key: str) -> Optional[dict]:
        return None

    def write_finding(self, finding: dict, standing: Any, **kw) -> None:
        self.written.append(finding)


_DEFAULT_TERMS = {"basic": {"terms_version": "1.0.0", "coverage_group": "basic",
                            "effective_from": "2024-01-01", "term_years": 4, "term_miles": 50_000,
                            "labor_rate_usd": 150.0}}


def _run(rows, warranty_terms=_DEFAULT_TERMS) -> tuple[dict, list]:
    f = _Fakes()
    stats = run_warranty_loop(
        candidate_rows=rows,
        computed_at=_COMPUTED_AT,
        standing_fn=f.standing_fn,
        lookup_live=f.lookup_live,
        write_finding=f.write_finding,
        warranty_terms=warranty_terms,
    )
    return stats, f.written


# ---------------------------------------------------------------------------
# W4: date-only deadline 2 days out → soon, expires_at = end of that day
# ---------------------------------------------------------------------------

class TestW4DateOnlyDeadline:
    def test_deadline_2days_out_is_soon(self):
        """DMS date-only 2026-10-11 is 2 days after computed_at → severity soon."""
        stats, written = _run([_row(filing_deadline="2026-10-11")])
        assert written, f"finding must be written; stats={stats}"
        assert written[0]["severity"] == "soon"

    def test_deadline_2days_out_expires_at_end_of_day(self):
        """expires_at = 2026-10-11T23:59:59Z (end of deadline day, not +3)."""
        _, written = _run([_row(filing_deadline="2026-10-11")])
        assert written, "finding must be written"
        assert written[0]["expires_at"] == "2026-10-11T23:59:59Z"

    def test_deadline_20days_out_is_routine(self):
        """2026-10-29 is 20 days out → routine (threshold 7)."""
        stats, written = _run([_row(filing_deadline="2026-10-29")])
        assert written, f"finding must be written; stats={stats}"
        assert written[0]["severity"] == "routine"

    def test_deadline_20days_out_expires_at_plus3(self):
        """Deadline far away → expires_at = computed_at + 3 = 2026-10-12T12:00:00Z."""
        _, written = _run([_row(filing_deadline="2026-10-29")])
        assert written, "finding must be written"
        assert written[0]["expires_at"] == "2026-10-12T12:00:00Z"

    def test_unparseable_deadline_refused_and_counted(self):
        """An unparseable deadline refuses the row (rows_refused >= 1, nothing written)."""
        stats, written = _run([_row(filing_deadline="not-a-date")])
        assert written == []
        assert stats["rows_refused"] >= 1

    def test_parse_deadline_raises_on_garbage(self):
        """`_parse_deadline` raises ValueError for non-ISO strings."""
        with pytest.raises(ValueError):
            _parse_deadline("not-a-date")


# ---------------------------------------------------------------------------
# W5: term statement contains actual years/miles
# ---------------------------------------------------------------------------

class TestW5TermStatement:
    def test_term_statement_contains_years(self):
        """disclosure_txt must embed the actual term_years value (4)."""
        wt = {"basic": _terms_row(term_years=4, term_miles=50_000)}
        _, written = _run([_row()], warranty_terms=wt)
        assert written, "finding must be written"
        assert "4 years" in written[0]["disclosure_txt"]

    def test_term_statement_contains_miles(self):
        """disclosure_txt must embed the actual term_miles value (50,000)."""
        wt = {"basic": _terms_row(term_years=4, term_miles=50_000)}
        _, written = _run([_row()], warranty_terms=wt)
        assert written, "finding must be written"
        assert "50,000 miles" in written[0]["disclosure_txt"]


# ---------------------------------------------------------------------------
# W5: estimated_value_basis names rate and terms version
# ---------------------------------------------------------------------------

class TestW5EstimatedValueBasis:
    def test_basis_contains_rate_value(self):
        """Basis must include the labor_rate_usd value (150.0)."""
        wt = {"basic": _terms_row(labor_rate_usd=150.0, terms_version="1.10.0")}
        _, written = _run([_row()], warranty_terms=wt)
        assert written, "finding must be written"
        basis = written[0]["recommended_action"]["estimated_value_basis"]
        assert "150" in basis

    def test_basis_contains_terms_version(self):
        """Basis must include the terms_version string (1.10.0)."""
        wt = {"basic": _terms_row(labor_rate_usd=150.0, terms_version="1.10.0")}
        _, written = _run([_row()], warranty_terms=wt)
        assert written, "finding must be written"
        basis = written[0]["recommended_action"]["estimated_value_basis"]
        assert "1.10.0" in basis


# ---------------------------------------------------------------------------
# W5: odometer evidence in miles
# ---------------------------------------------------------------------------

class TestW5OdometerMiles:
    def test_odometer_at_repair_is_miles_rounded(self):
        """odometer_at_repair = round(odometer_km / 1.609344), not the raw km."""
        ev = build_warranty_evidence(_row(odometer_km=45_000), "rules")
        cov = next(e["warranty.coverage"] for e in ev if "warranty.coverage" in e)
        expected = round(45_000 / _KM_TO_MILES)
        assert cov["odometer_at_repair"] == expected


# ---------------------------------------------------------------------------
# W5: semver pick — 1.10.0 beats 1.9.0
# ---------------------------------------------------------------------------

class TestW5SemverPick:
    """Terms come from the DMS lake (dms_<stage>.warranty_terms), strings per adp_source."""

    @staticmethod
    def _rows():
        def _r(v, grp, ty, tm, rate, eff):
            return {"terms_version": v, "coverage_group": grp, "effective_from": eff,
                    "term_years": str(ty), "term_miles": str(tm), "labor_rate_usd": str(rate)}
        return [
            _r("1.9.0", "basic", 3, 40_000, 120.0, "2024-01-01"),
            _r("1.10.0", "basic", 4, 50_000, 150.0, "2024-06-01"),
            _r("1.10.0", "hv_battery", 8, 100_000, 150.0, "2024-06-01"),
            _r("v2", "basic", 9, 1, 1.0, "2024-07-01"),          # unparseable: dropped
        ]

    def test_semver_pick_chooses_1_10_0(self):
        """1.10.0 > 1.9.0 numerically; lexicographic order gives 1.9.0."""
        seen = {}

        def _fake_run_query(sql, **kw):
            seen["sql"], seen["kw"] = sql, kw
            return self._rows()

        result = select_warranty_terms("staging", athena_client=object(), workgroup="wg",
                                       results_s3_path="s3://b/p", _run_query=_fake_run_query)
        assert result["basic"]["terms_version"] == "1.10.0"
        assert result["basic"]["term_years"] == 4
        assert result["basic"]["term_miles"] == 50_000
        assert result["basic"]["labor_rate_usd"] == 150.0
        assert set(result) == {"basic", "hv_battery"}
        assert "FROM dms_staging.warranty_terms" in seen["sql"]
        assert "CAST(effective_from AS DATE) <= current_date" in seen["sql"]
        assert seen["kw"]["workgroup"] == "wg"

    def test_no_usable_rows_gives_empty(self):
        rows = [{"terms_version": "x.y", "coverage_group": "basic", "term_years": "3",
                 "term_miles": "1", "labor_rate_usd": "1"}]
        assert select_warranty_terms("staging", athena_client=None, workgroup="w",
                                     results_s3_path="s3://b",
                                     _run_query=lambda sql, **kw: rows) == {}


class TestRowWithoutTermsRefused:
    """§ 2.5: no current terms row for the coverage group -> refused and counted, never a
    Finding whose disclosure lacks the term statement."""

    def test_missing_terms_refused_and_counted(self):
        stats, written = _run([_row()], warranty_terms={})
        assert written == []
        assert stats["rows_without_terms"] == 1
        assert stats["rows_refused"] == 1

    def test_control_terms_present_writes_with_term_statement(self):
        stats, written = _run([_row()])
        assert len(written) == 1 and stats["rows_without_terms"] == 0
        assert written[0]["disclosure_txt"].startswith("Basic warranty: 4 years / 50,000 miles")
        assert written[0]["disclosure_txt"].endswith("DMS decides coverage when the claim is filed.")
