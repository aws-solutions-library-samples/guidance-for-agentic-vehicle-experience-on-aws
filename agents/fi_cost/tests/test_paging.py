"""
Item 6 of task 1.4 — NextToken paging follows all pages to the end (D6 step 1).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1, § D11 (inputs_hash)

Lesson from the Tier 2 health agent live-verification (agentic-tiers.md):
"single-page GetQueryResults silently truncating at the 4th tire VIN — which
meant M6 idempotency was not merely untested but untrue."

The selection query in D6 step 1 must follow NextToken to the end.  A stub
returning THREE pages asserts that all vehicles from all pages are returned.

Athena is stubbed; the repo-root conftest blocks real AWS calls.

All tests are xfail(strict=True).  Group 3 makes them pass.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_three_page_athena_stub():
    """Athena stub returning rows split across three pages via NextToken.

    Pages:
      Page 1: VINs A, B    → NextToken="tok-2"
      Page 2: VINs C, D    → NextToken="tok-3"
      Page 3: VINs E, F    → no NextToken (last page)
    """
    _PAGES = [
        {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "vin"}, {"VarCharValue": "ratio_cohort"}]},
                    {"Data": [{"VarCharValue": "MRD-PAGE1-A"}, {"VarCharValue": "1.8"}]},
                    {"Data": [{"VarCharValue": "MRD-PAGE1-B"}, {"VarCharValue": "1.7"}]},
                ],
            },
            "NextToken": "tok-2",
        },
        {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "MRD-PAGE2-C"}, {"VarCharValue": "2.1"}]},
                    {"Data": [{"VarCharValue": "MRD-PAGE2-D"}, {"VarCharValue": "1.9"}]},
                ],
            },
            "NextToken": "tok-3",
        },
        {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "MRD-PAGE3-E"}, {"VarCharValue": "1.6"}]},
                    {"Data": [{"VarCharValue": "MRD-PAGE3-F"}, {"VarCharValue": "1.5"}]},
                ],
            },
            # no NextToken — last page
        },
    ]
    _ALL_EXPECTED_VINS = {
        "MRD-PAGE1-A", "MRD-PAGE1-B",
        "MRD-PAGE2-C", "MRD-PAGE2-D",
        "MRD-PAGE3-E", "MRD-PAGE3-F",
    }

    query_pages = iter(_PAGES)
    result_pages: dict = {}

    def _start_query(**kwargs):
        return {"QueryExecutionId": "qid-paging-test"}

    def _get_query_execution(**kwargs):
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    def _get_query_results(**kwargs):
        token = kwargs.get("NextToken")
        if token is None:
            return next(iter(_PAGES))   # first page
        elif token == "tok-2":
            return _PAGES[1]
        elif token == "tok-3":
            return _PAGES[2]
        return {"ResultSet": {"Rows": []}}

    stub = MagicMock()
    stub.start_query_execution = _start_query
    stub.get_query_execution = _get_query_execution
    stub.get_query_results = _get_query_results

    return stub, _ALL_EXPECTED_VINS


# ---------------------------------------------------------------------------
# Paging: selection reads all three pages
# ---------------------------------------------------------------------------


def test_selection_follows_next_token_to_end():
    """select_anomalies() must follow all NextToken pages; stopping early loses VINs.

    Mutation target: truncate to one page → the test must fail.
    """
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    athena_stub, expected_vins = _make_three_page_athena_stub()

    results = select_anomalies(
        athena_client=athena_stub,
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/",
        max_diagnoses_per_run=100,
        latest_complete_month="2026-09",
    )

    returned_vins = {row["vin"] for row in results}
    missing = expected_vins - returned_vins
    assert not missing, (
        f"select_anomalies() dropped VINs from later pages: {missing!r}. "
        "The selection must follow NextToken to the end of the result set."
    )


def test_selection_returns_all_six_vehicles_from_three_pages():
    """Exactly 6 vehicles returned from the 3-page stub (no duplicates, none dropped)."""
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    athena_stub, expected_vins = _make_three_page_athena_stub()

    results = select_anomalies(
        athena_client=athena_stub,
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/",
        max_diagnoses_per_run=100,
        latest_complete_month="2026-09",
    )

    assert len(results) == 6, (
        f"Expected 6 vehicles across 3 pages, got {len(results)}"
    )


def test_selection_respects_max_diagnoses_cap():
    """max_diagnoses_per_run does NOT cap the selection query — all 6 rows are returned.

    CHANGED by decisions.md 2026-10-05, item 6:
        "max_diagnoses_per_run caps model calls after D6 step 3's skip,
         not the selection query."
    The previous test expected len(results)==3 when max_diagnoses_per_run=3.
    That was contradicted by the 2026-10-05 decision: the query returns the full
    anomaly set so later daily runs can reach vehicles earlier runs skipped.
    The cap is applied by the runner (agent.py) AFTER the unchanged-skip check.

    This test now verifies that select_anomalies() returns ALL 6 vehicles even
    when max_diagnoses_per_run is set to a value smaller than the page count.
    """
    from agents.fi_cost.selection import select_anomalies  # noqa: F401

    athena_stub, expected_vins = _make_three_page_athena_stub()

    # max_diagnoses_per_run=3 must NOT cap the query: all 6 vehicles returned
    results = select_anomalies(
        athena_client=athena_stub,
        workgroup="cvx-staging-analytics",
        results_s3_path="s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/",
        max_diagnoses_per_run=3,
        latest_complete_month="2026-09",
    )

    assert len(results) == 6, (
        f"select_anomalies() must return all 6 vehicles regardless of "
        f"max_diagnoses_per_run (got {len(results)}).  "
        "The cap is applied by the runner after the unchanged-skip, not here. "
        "(decisions.md 2026-10-05, item 6)"
    )

    returned_vins = {row["vin"] for row in results}
    assert returned_vins == expected_vins, (
        "All 6 VINs from all three pages must be returned."
    )


# ---------------------------------------------------------------------------
# Mutation note (for decisions.md when Group 3 makes these pass):
#   Truncate select_anomalies() to one page (stop after first GetQueryResults
#   if NextToken is present). test_selection_follows_next_token_to_end must FAIL.
# ---------------------------------------------------------------------------
