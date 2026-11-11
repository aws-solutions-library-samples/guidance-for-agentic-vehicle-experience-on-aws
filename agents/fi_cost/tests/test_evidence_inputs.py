"""
Tests for evidence.py input validation and compute_inputs_hash number normalisation.

Covers fixes from PREFIX.md 2026-10-05:
  Fix 1b — compute_inputs_hash uses fixed 6-decimal-place number formatting so a
            last-digit difference from a distributed AVG cannot change the hash.
  Fix 3  — gather_evidence and select_anomalies validate stage / VINs before any
            Athena call; ValueError names the first bad VIN.

Mutation targets (one per item):
  M-FMT-below:   change threshold from 1e-6 to 1e-4 in normalise → dicts differing
                 only below 1e-6 would hash differently → test_below_1e6_hashes_equal
                 fails.
  M-FMT-above:   drop round(f, 6) → 1e-4 difference collapses into the same string →
                 test_differs_at_1e4_hashes_differently fails.
  M-STR-EQ:      hash "0.25" and 0.25 differently → test_numeric_string_equal_to_float
                 fails.
  M-BOOL:        treat True as float(True)=1.0 → test_bool_not_treated_as_number fails.
  M-STAGE-SEL:   remove stage validation in select_anomalies → test_stage_invalid_
                 select fails.
  M-STAGE-EV:    remove stage validation in gather_evidence → test_stage_invalid_
                 gather fails.
  M-VIN-CHARS:   remove VIN character validation → test_vin_invalid_chars fails.
  M-VIN-QUOTE:   allow VIN containing a quote → test_vin_with_quote_is_rejected fails.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base_evidence(**overrides) -> dict:
    base = {
        "vin": "MRD00000000000001",
        "cost.cost_per_mile": 0.45,
        "ratio_cohort": 1.7,
    }
    base.update(overrides)
    return base


def _make_athena_stub_empty() -> MagicMock:
    """Athena stub that returns zero rows (used to confirm validation fires before call)."""
    def _start(**kwargs):
        return {"QueryExecutionId": "qid-val-test"}

    def _get_exec(**kwargs):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def _get_results(**kwargs):
        # Header row only → zero data rows
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "vin"}]}]}}

    stub = MagicMock()
    stub.start_query_execution = _start
    stub.get_query_execution = _get_exec
    stub.get_query_results = _get_results
    return stub


# ---------------------------------------------------------------------------
# Fix 1b — fixed number formatting in compute_inputs_hash (D11)
# ---------------------------------------------------------------------------


def test_below_1e6_difference_hashes_equal():
    """Two evidence dicts differing only below 1e-6 must produce identical hashes.

    D11 (fix 1b): numbers are rounded to 6 decimal places before hashing so that
    a distributed AVG returning e.g. 1.2345678 and 1.2345679 produces the same hash.

    Mutation M-FMT-below: if the threshold is raised (e.g. comparing raw floats),
    these two dicts would hash differently and this assertion fails.
    """
    from agents.fi_cost.evidence import compute_inputs_hash

    e1 = _make_base_evidence(**{"ratio_cohort": 1.700000100000})
    e2 = _make_base_evidence(**{"ratio_cohort": 1.700000199999})
    assert compute_inputs_hash(e1) == compute_inputs_hash(e2), (
        "Two evidence dicts differing only below 1e-6 must hash identically (D11). "
        "Mutation M-FMT-below: without rounding to 6dp the hashes differ."
    )


def test_difference_at_1e4_hashes_differently():
    """Two evidence dicts differing at 1e-4 must produce different hashes.

    Mutation M-FMT-above: if round(f, 6) is dropped, 1e-4 differences collapse
    into the same representation and the hashes become equal → this assertion fails.
    """
    from agents.fi_cost.evidence import compute_inputs_hash

    e1 = _make_base_evidence(**{"ratio_cohort": 1.700000})
    e2 = _make_base_evidence(**{"ratio_cohort": 1.700100})
    assert compute_inputs_hash(e1) != compute_inputs_hash(e2), (
        "Two evidence dicts differing at 1e-4 must hash differently (D11). "
        "Mutation M-FMT-above: without round(f,6) the hashes become equal."
    )


def test_numeric_string_equal_to_float():
    """A numeric string '0.25' and a float 0.25 must hash identically (D11).

    Athena returns all values as strings; the Python wrapper casts them.
    Both representations must produce the same hash so a mid-cycle type change
    (e.g. an extra cast in the query) does not trigger a spurious re-diagnosis.

    Mutation M-STR-EQ: hash string and float differently → these dicts hash
    differently and this assertion fails.
    """
    from agents.fi_cost.evidence import compute_inputs_hash

    e_str = _make_base_evidence(**{"ratio_cohort": "0.25"})
    e_float = _make_base_evidence(**{"ratio_cohort": 0.25})
    assert compute_inputs_hash(e_str) == compute_inputs_hash(e_float), (
        "Numeric string '0.25' and float 0.25 must hash identically (D11). "
        "Mutation M-STR-EQ: hashing them differently causes spurious re-diagnoses."
    )


def test_bool_not_treated_as_number():
    """True must NOT be hashed as the number 1 (D11 / _normalize_value_for_hash).

    Booleans represent categorical flags (is_outlier, is_energy_outlier), not
    quantities.  Treating True as 1 would make an evidence dict with
    {'flag': True} hash identically to {'flag': 1}, hiding a data-type change.

    Mutation M-BOOL: normalise booleans as float(True)=1.0 → hash of True equals
    hash of 1 → these dicts hash identically and this assertion fails.
    """
    from agents.fi_cost.evidence import compute_inputs_hash

    e_bool = _make_base_evidence(**{"is_outlier": True})
    e_int  = _make_base_evidence(**{"is_outlier": 1})
    assert compute_inputs_hash(e_bool) != compute_inputs_hash(e_int), (
        "True must not be treated as the number 1 in compute_inputs_hash (D11). "
        "Mutation M-BOOL: treating bool as float causes True == 1 in the hash."
    )


def test_none_value_hashes_stably():
    """A None value in the evidence dict must not raise and must hash stably."""
    from agents.fi_cost.evidence import compute_inputs_hash

    e = _make_base_evidence(**{"cost.fleet_median_miles": None})
    h1 = compute_inputs_hash(e)
    h2 = compute_inputs_hash(e)
    assert h1 == h2, "None values must hash stably"


def test_non_numeric_string_hashes_unchanged():
    """A non-numeric string such as 'staging' must hash without modification."""
    from agents.fi_cost.evidence import compute_inputs_hash

    e1 = _make_base_evidence(**{"cost.method": "own 6-month mean and fleet-model median"})
    e2 = _make_base_evidence(**{"cost.method": "own 6-month mean and fleet-model median"})
    assert compute_inputs_hash(e1) == compute_inputs_hash(e2)


# ---------------------------------------------------------------------------
# Fix 3 — stage validation in select_anomalies
# ---------------------------------------------------------------------------


def test_stage_valid_values_for_select_anomalies():
    """'staging' and 'prod' must both pass stage validation without error."""
    from agents.fi_cost.selection import select_anomalies

    for valid_stage in ("staging", "prod"):
        stub = _make_athena_stub_empty()
        # Should not raise — just return []
        result = select_anomalies(
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            max_diagnoses_per_run=50,
            latest_complete_month="2026-09",
            stage=valid_stage,
        )
        assert isinstance(result, list), f"stage={valid_stage!r} must return a list"


def test_stage_invalid_raises_before_athena_select():
    """An invalid stage must raise ValueError before any Athena call.

    Mutation M-STAGE-SEL: remove the stage validation → no ValueError is raised
    and the Athena stub is called with a bad stage name → this test fails.
    """
    from agents.fi_cost.selection import select_anomalies

    call_count = []

    def _counting_start(**kwargs):
        call_count.append(kwargs)
        return {"QueryExecutionId": "qid-bad-stage"}

    stub = MagicMock()
    stub.start_query_execution = _counting_start

    with pytest.raises(ValueError, match="stage"):
        select_anomalies(
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            max_diagnoses_per_run=50,
            latest_complete_month="2026-09",
            stage="development",  # invalid
        )

    assert len(call_count) == 0, (
        "No Athena call must be made when stage is invalid. "
        "Mutation M-STAGE-SEL: without stage validation the call proceeds."
    )


def test_stage_invalid_examples_for_select():
    """A variety of invalid stage values must all raise ValueError."""
    from agents.fi_cost.selection import select_anomalies

    for bad_stage in ("dev", "staging ", "STAGING", "beta", "", "staging;DROP TABLE"):
        with pytest.raises(ValueError):
            select_anomalies(
                athena_client=MagicMock(),
                workgroup="cvx-staging-analytics",
                results_s3_path="s3://bucket/path/",
                max_diagnoses_per_run=50,
                latest_complete_month="2026-09",
                stage=bad_stage,
            )


# ---------------------------------------------------------------------------
# Fix 3 — stage validation in gather_evidence
# ---------------------------------------------------------------------------


def test_stage_valid_values_for_gather_evidence():
    """'staging' and 'prod' must pass stage validation in gather_evidence."""
    from agents.fi_cost.evidence import gather_evidence

    vins = ["MRD00000000000001"]
    for valid_stage in ("staging", "prod"):
        stub = _make_athena_stub_empty()
        result = gather_evidence(
            selected_vins=vins,
            year_month="2026-09",
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            stage=valid_stage,
        )
        assert isinstance(result, list), f"stage={valid_stage!r} must return a list"


def test_stage_invalid_raises_before_athena_gather():
    """An invalid stage must raise ValueError before any Athena call in gather_evidence.

    Mutation M-STAGE-EV: remove stage validation in gather_evidence → no ValueError,
    the stub is called → this test fails.
    """
    from agents.fi_cost.evidence import gather_evidence

    call_count = []

    def _counting_start(**kwargs):
        call_count.append(kwargs)
        return {"QueryExecutionId": "qid-bad-stage-ev"}

    stub = MagicMock()
    stub.start_query_execution = _counting_start

    with pytest.raises(ValueError, match="stage"):
        gather_evidence(
            selected_vins=["MRD00000000000001"],
            year_month="2026-09",
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            stage="uat",  # invalid
        )

    assert len(call_count) == 0, (
        "No Athena call must be made when stage is invalid. "
        "Mutation M-STAGE-EV: without stage validation the call proceeds."
    )


# ---------------------------------------------------------------------------
# Fix 3 — VIN validation in gather_evidence
# ---------------------------------------------------------------------------


def test_valid_vin_passes_gather_evidence():
    """A well-formed 17-character VIN must pass validation and reach Athena."""
    from agents.fi_cost.evidence import gather_evidence

    stub = _make_athena_stub_empty()
    result = gather_evidence(
        selected_vins=["MRD00000000000001"],
        year_month="2026-09",
        athena_client=stub,
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://bucket/path/",
        stage="staging",
    )
    assert isinstance(result, list)


def test_vin_wrong_length_raises():
    """A VIN shorter or longer than 17 characters must raise ValueError."""
    from agents.fi_cost.evidence import gather_evidence

    for bad_vin in ("MRD0000000000001", "MRD000000000000012"):  # 16 and 18 chars
        with pytest.raises(ValueError, match=bad_vin[:6]):
            gather_evidence(
                selected_vins=[bad_vin],
                year_month="2026-09",
                athena_client=_make_athena_stub_empty(),
                workgroup="cvx-staging-analytics",
                results_s3_path="s3://bucket/path/",
                stage="staging",
            )


def test_vin_invalid_chars_raises():
    """A VIN containing I, O, or Q must raise ValueError (ISO 3779 exclusions).

    Mutation M-VIN-CHARS: remove the VIN character check → no ValueError, the
    invalid VIN is interpolated into SQL and this test fails.
    """
    from agents.fi_cost.evidence import gather_evidence

    for bad_vin in (
        "MRDI00000000000I1",   # contains I
        "MRDO00000000000O1",   # contains O
        "MRDQ00000000000Q1",   # contains Q
    ):
        with pytest.raises(ValueError):
            gather_evidence(
                selected_vins=[bad_vin],
                year_month="2026-09",
                athena_client=_make_athena_stub_empty(),
                workgroup="cvx-staging-analytics",
                results_s3_path="s3://bucket/path/",
                stage="staging",
            )


def test_vin_with_quote_is_rejected():
    """A VIN containing a single quote must raise ValueError before any Athena call.

    A VIN containing a quote would break the quoted VIN list in the SQL template
    and could enable SQL injection.  Validation must reject it unconditionally.

    Mutation M-VIN-QUOTE: allow quotes in VINs → no ValueError, the malformed SQL
    is sent to Athena → this test fails.
    """
    from agents.fi_cost.evidence import gather_evidence

    # VIN with embedded quote — must be rejected before Athena call
    bad_vin = "MRD000000000A'001"

    call_count = []

    def _counting_start(**kwargs):
        call_count.append(kwargs)
        return {"QueryExecutionId": "qid-vin-quote"}

    def _get_exec(**kwargs):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def _get_results(**kwargs):
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "vin"}]}]}}

    stub = MagicMock()
    stub.start_query_execution = _counting_start
    stub.get_query_execution = _get_exec
    stub.get_query_results = _get_results

    with pytest.raises(ValueError):
        gather_evidence(
            selected_vins=[bad_vin],
            year_month="2026-09",
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            stage="staging",
        )

    assert len(call_count) == 0, (
        "No Athena call must be made when a VIN contains a quote. "
        "Mutation M-VIN-QUOTE: without VIN validation the call proceeds."
    )


def test_vin_error_names_first_bad_vin():
    """The ValueError must name the first invalid VIN in its message."""
    from agents.fi_cost.evidence import gather_evidence

    bad_vin = "MRDXXX00000000I01"  # contains I

    with pytest.raises(ValueError) as exc_info:
        gather_evidence(
            selected_vins=["MRD00000000000001", bad_vin, "MRD00000000000002"],
            year_month="2026-09",
            athena_client=_make_athena_stub_empty(),
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            stage="staging",
        )

    assert bad_vin in str(exc_info.value), (
        "The ValueError must name the first invalid VIN so it is easy to identify. "
        "A valid VIN before the invalid one must not suppress the error."
    )


def test_vin_validation_fires_before_athena_call():
    """VIN validation must raise ValueError before the first Athena API call."""
    from agents.fi_cost.evidence import gather_evidence

    call_count = []

    def _counting_start(**kwargs):
        call_count.append(kwargs)
        return {"QueryExecutionId": "qid-vin-order"}

    def _get_exec(**kwargs):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def _get_results(**kwargs):
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": "vin"}]}]}}

    stub = MagicMock()
    stub.start_query_execution = _counting_start
    stub.get_query_execution = _get_exec
    stub.get_query_results = _get_results

    with pytest.raises(ValueError):
        gather_evidence(
            selected_vins=["MRD000000000000O1"],  # contains O → invalid
            year_month="2026-09",
            athena_client=stub,
            workgroup="cvx-staging-analytics",
            results_s3_path="s3://bucket/path/",
            stage="staging",
        )

    assert len(call_count) == 0, (
        "No Athena call must be made when any VIN is invalid."
    )
