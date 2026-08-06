"""Unit tests for agents/tier2/adp_source.py.

Tests cover:
  - Happy path: query succeeds, rows normalised correctly
  - Timeout path: poll ceiling exceeded raises AthenaTimeoutError
  - Empty-result path: SUCCEEDED but no data rows → empty list
  - FAILED query raises AthenaCursorError
  - CANCELLED query raises AthenaCursorError

All tests use a stubbed boto3 Athena client (botocore.stub.Stubber).
No SQL, no ADP column names appear in this module — they belong in the
component modules per spec § D3/D3a.

No pytest-asyncio required: all tests are synchronous.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from agents.tier2.adp_source import (
    AthenaTimeoutError,
    AthenaCursorError,
    AthenaResultTooLargeError,
    run_query,
    _normalise,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKGROUP = "cvx-staging-analytics"
RESULTS_PATH = "s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/"
SAMPLE_SQL = "SELECT col_a, col_b FROM some_table"  # no real ADP column names
EXECUTION_ID = "aabbccdd-1234-5678-9abc-def012345678"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _athena_client() -> tuple[Any, Stubber]:
    """Return a stubbed boto3 Athena client."""
    client = boto3.client("athena", region_name="us-east-1")
    stubber = Stubber(client)
    return client, stubber


def _start_resp(execution_id: str = EXECUTION_ID) -> dict:
    return {"QueryExecutionId": execution_id, "ResponseMetadata": {}}


def _execution_status(state: str, reason: str | None = None) -> dict:
    status: dict = {"State": state}
    if reason:
        status["StateChangeReason"] = reason
    return {
        "QueryExecution": {
            "QueryExecutionId": EXECUTION_ID,
            "Status": status,
        },
        "ResponseMetadata": {},
    }


def _results_resp(
    columns: list[str],
    data_rows: list[list[str | None]],
) -> dict:
    """Build a GetQueryResults response.  First row is always the header."""
    header_row = {"Data": [{"VarCharValue": c} for c in columns]}
    rows = [header_row]
    for dr in data_rows:
        cells = [{"VarCharValue": v} if v is not None else {} for v in dr]
        rows.append({"Data": cells})
    return {
        "ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}},
        "ResponseMetadata": {},
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_query_returns_normalised_rows():
    """SUCCEEDED query with two data rows returns a list of dicts."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp(), expected_params={
        "QueryString": SAMPLE_SQL,
        "QueryExecutionContext": {"Catalog": "AwsDataCatalog"},
        "WorkGroup": WORKGROUP,
        "ResultConfiguration": {"OutputLocation": RESULTS_PATH},
    })
    # First poll: RUNNING
    stubber.add_response("get_query_execution", _execution_status("RUNNING"),
                         expected_params={"QueryExecutionId": EXECUTION_ID})
    # Second poll: SUCCEEDED
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"),
                         expected_params={"QueryExecutionId": EXECUTION_ID})
    stubber.add_response(
        "get_query_results",
        _results_resp(["col_a", "col_b"], [["val1", "val2"], ["val3", "val4"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,  # no real sleep in tests
        )

    assert rows == [
        {"col_a": "val1", "col_b": "val2"},
        {"col_a": "val3", "col_b": "val4"},
    ]
    stubber.assert_no_pending_responses()


def test_run_query_single_data_row():
    """One data row is returned as a single-element list."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(["x"], [["42"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    assert rows == [{"x": "42"}]


def test_run_query_propagates_correct_s3_path():
    """OutputLocation in the start call exactly matches the caller's results_s3_path."""
    custom_path = "s3://my-results-bucket/prefix/"
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp(), expected_params={
        "QueryString": SAMPLE_SQL,
        "QueryExecutionContext": {"Catalog": "AwsDataCatalog"},
        "WorkGroup": WORKGROUP,
        "ResultConfiguration": {"OutputLocation": custom_path},
    })
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response("get_query_results",
                         _results_resp(["n"], [["1"]]),
                         expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000})

    with stubber:
        run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=custom_path,
            poll_interval=0,
        )

    stubber.assert_no_pending_responses()


def test_run_query_null_cell_becomes_none():
    """A cell absent from Athena's response (NULL) is represented as None."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    # Build response with a cell that has no VarCharValue (NULL)
    stubber.add_response(
        "get_query_results",
        _results_resp(["a", "b"], [["present", None]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    assert rows[0]["a"] == "present"
    assert rows[0]["b"] is None


# ---------------------------------------------------------------------------
# Empty-result path
# ---------------------------------------------------------------------------


def test_run_query_empty_result_set_returns_empty_list():
    """SUCCEEDED with no data rows (only header) returns []."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    # Header only — no data rows
    stubber.add_response(
        "get_query_results",
        _results_resp(["col_a"], []),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    assert rows == []
    stubber.assert_no_pending_responses()


def test_run_query_completely_empty_rows_list_returns_empty():
    """GetQueryResults returning zero rows (no header) returns []."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        {"ResultSet": {"Rows": []}, "ResponseMetadata": {}},
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    assert rows == []


# ---------------------------------------------------------------------------
# Timeout path
# ---------------------------------------------------------------------------


def test_run_query_timeout_raises_athena_timeout_error():
    """Exceeding max_polls without SUCCEEDED raises AthenaTimeoutError."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    # Return RUNNING for every poll (3 polls = max_polls=3 with no SUCCEEDED)
    for _ in range(3):
        stubber.add_response("get_query_execution", _execution_status("RUNNING"))

    with stubber:
        with pytest.raises(AthenaTimeoutError):
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                max_polls=3,
                poll_interval=0,
            )

    stubber.assert_no_pending_responses()


def test_run_query_timeout_error_mentions_execution_id():
    """AthenaTimeoutError message includes the execution ID for debugging."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("RUNNING"))

    with stubber:
        with pytest.raises(AthenaTimeoutError) as exc_info:
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                max_polls=1,
                poll_interval=0,
            )

    assert EXECUTION_ID in str(exc_info.value)


# ---------------------------------------------------------------------------
# FAILED / CANCELLED paths
# ---------------------------------------------------------------------------


def test_run_query_failed_state_raises_athena_cursor_error():
    """FAILED terminal state raises AthenaCursorError."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response(
        "get_query_execution",
        _execution_status("FAILED", reason="SCHEMA_NOT_FOUND: adp_staging_tire_health"),
    )

    with stubber:
        with pytest.raises(AthenaCursorError) as exc_info:
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                poll_interval=0,
            )

    # Error message must include the state and ideally the reason
    assert "FAILED" in str(exc_info.value)
    stubber.assert_no_pending_responses()


def test_run_query_cancelled_state_raises_athena_cursor_error():
    """CANCELLED terminal state also raises AthenaCursorError."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("CANCELLED"))

    with stubber:
        with pytest.raises(AthenaCursorError) as exc_info:
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                poll_interval=0,
            )

    assert "CANCELLED" in str(exc_info.value)


def test_run_query_failed_reason_included_in_error():
    """The StateChangeReason is surfaced in the AthenaCursorError message."""
    reason = "Disk quota exceeded on Athena workgroup"
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("FAILED", reason=reason))

    with stubber:
        with pytest.raises(AthenaCursorError) as exc_info:
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                poll_interval=0,
            )

    assert reason in str(exc_info.value)


# ---------------------------------------------------------------------------
# _normalise unit tests (pure function — no Athena call)
# ---------------------------------------------------------------------------


def test_normalise_empty_list():
    assert _normalise([]) == []


def test_normalise_header_only():
    """A result set with a header but no data rows returns []."""
    header_row = {"Data": [{"VarCharValue": "col_a"}, {"VarCharValue": "col_b"}]}
    assert _normalise([header_row]) == []


def test_normalise_single_data_row():
    header_row = {"Data": [{"VarCharValue": "col_a"}, {"VarCharValue": "col_b"}]}
    data_row = {"Data": [{"VarCharValue": "v1"}, {"VarCharValue": "v2"}]}
    result = _normalise([header_row, data_row])
    assert result == [{"col_a": "v1", "col_b": "v2"}]


def test_normalise_multiple_data_rows():
    header_row = {"Data": [{"VarCharValue": "x"}]}
    rows = [header_row] + [{"Data": [{"VarCharValue": str(i)}]} for i in range(5)]
    result = _normalise(rows)
    assert len(result) == 5
    assert result[0] == {"x": "0"}
    assert result[4] == {"x": "4"}


def test_normalise_null_cell_is_none():
    header_row = {"Data": [{"VarCharValue": "col_a"}, {"VarCharValue": "col_b"}]}
    data_row = {"Data": [{"VarCharValue": "present"}, {}]}  # second cell is NULL
    result = _normalise([header_row, data_row])
    assert result[0]["col_a"] == "present"
    assert result[0]["col_b"] is None


# ---------------------------------------------------------------------------
# No SQL or ADP column names in this module
# ---------------------------------------------------------------------------


def _collect_non_docstring_strings(tree: "ast.Module") -> list[str]:
    """Return all string constant values that are NOT module/class/function docstrings.

    Docstrings legitimately describe what a module does not contain (e.g. "No SQL"),
    so scanning them would produce false positives on English prose.  We only care
    about runtime string literals where SQL could actually appear — query strings,
    error messages, dict keys, etc.

    A docstring is the first statement of a module/class/function body when that
    statement is a bare ``ast.Expr`` wrapping an ``ast.Constant`` of type ``str``.
    """
    import ast

    docstring_nodes: set[int] = set()

    def _mark_docstrings(body: list) -> None:
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))

    _mark_docstrings(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _mark_docstrings(node.body)

    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_nodes:
                result.append(node.value)
    return result


def test_adp_source_contains_no_sql_keywords():
    """Verify adp_source.py contains no SQL or ADP column names.

    The hard constraint from spec § D3/D3a: SQL and ADP column names belong in
    the component modules (agents/health/), not here. This test scans the source
    text so the constraint is enforced, not just asserted by convention.

    SQL keywords are checked against non-docstring string literals only (via
    ``ast``), so English prose in docstrings (e.g. "extracted from ...") does not
    produce false positives.  Matching is case-insensitive so mixed-case variants
    (e.g. ``SeLeCt``, ``SeLeCt ``, ``SeLeCt col``) are caught with the same
    certainty as canonical uppercase SQL.

    ADP column names are checked against the full lowercased source text — none
    of the forbidden identifiers appear legitimately in English prose.
    """
    import ast
    import pathlib

    source_path = pathlib.Path(__file__).resolve().parents[1] / "adp_source.py"
    source_text = source_path.read_text()
    tree = ast.parse(source_text)

    # --- SQL keywords — check non-docstring string literals only ---
    # Using AST avoids false positives from English "from"/"where" in docstrings
    # while still catching any casing variant in a runtime string literal.
    non_docstring_strings = _collect_non_docstring_strings(tree)
    combined_literals = " ".join(non_docstring_strings).lower()

    forbidden_sql = ["select ", " from ", " where ", " join ", " group by ", " order by "]
    for keyword in forbidden_sql:
        assert keyword not in combined_literals, (
            f"adp_source.py contains SQL keyword {keyword!r} in a string literal "
            "(case-insensitive) — SQL belongs in the component modules (agents/health/)"
        )

    # --- ADP column names — full source text, case-insensitive ---
    # None of these identifiers appear in legitimate English prose inside
    # adp_source.py, so a full-text lowercase scan is safe and catches any
    # capitalisation variant (e.g. "Tire_Health", "TIRE_HEALTH").
    source_lower = source_text.lower()
    forbidden_columns = [
        "state_of_health_pct",
        "battery_age_days",
        "needs_replacement",
        "wear_category",
        "wear_rate_mm_per_1k_km",
        "energy_usage",
        "tire_health",
    ]
    for col in forbidden_columns:
        assert col not in source_lower, (
            f"adp_source.py contains ADP column name {col!r} (case-insensitive) — "
            "column names belong in the component modules (agents/health/)"
        )


# ---------------------------------------------------------------------------
# Pagination — NextToken followed to exhaustion (T6a.1)
# ---------------------------------------------------------------------------


def _results_resp_with_next_token(
    columns: list[str],
    data_rows: list[list[str | None]],
    next_token: str | None = None,
) -> dict:
    """Build a GetQueryResults response that may include a NextToken."""
    header_row = {"Data": [{"VarCharValue": c} for c in columns]}
    rows = [header_row]
    for dr in data_rows:
        cells = [{"VarCharValue": v} if v is not None else {} for v in dr]
        rows.append({"Data": cells})
    resp: dict = {
        "ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}},
        "ResponseMetadata": {},
    }
    if next_token is not None:
        resp["NextToken"] = next_token
    return resp


def _results_resp_page2(
    columns: list[str],
    data_rows: list[list[str | None]],
    next_token: str | None = None,
) -> dict:
    """Build a subsequent-page GetQueryResults response (no header row)."""
    rows = []
    for dr in data_rows:
        cells = [{"VarCharValue": v} if v is not None else {} for v in dr]
        rows.append({"Data": cells})
    resp: dict = {
        "ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}},
        "ResponseMetadata": {},
    }
    if next_token is not None:
        resp["NextToken"] = next_token
    return resp


def test_run_query_follows_next_token_two_pages():
    """A two-page result set is returned complete; header stripped exactly once."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))

    # Page 1: header + 2 data rows, has NextToken
    stubber.add_response(
        "get_query_results",
        _results_resp_with_next_token(
            ["col_a", "col_b"],
            [["r1c1", "r1c2"], ["r2c1", "r2c2"]],
            next_token="token-page-2",
        ),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )
    # Page 2: data rows only (no header), no NextToken
    stubber.add_response(
        "get_query_results",
        _results_resp_page2(
            ["col_a", "col_b"],  # ignored by the paginator; header came from page 1
            [["r3c1", "r3c2"], ["r4c1", "r4c2"]],
        ),
        expected_params={
            "QueryExecutionId": EXECUTION_ID,
            "MaxResults": 1000,
            "NextToken": "token-page-2",
        },
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    # All four data rows returned; header not duplicated
    assert len(rows) == 4
    assert rows[0] == {"col_a": "r1c1", "col_b": "r1c2"}
    assert rows[1] == {"col_a": "r2c1", "col_b": "r2c2"}
    assert rows[2] == {"col_a": "r3c1", "col_b": "r3c2"}
    assert rows[3] == {"col_a": "r4c1", "col_b": "r4c2"}
    stubber.assert_no_pending_responses()


def test_run_query_follows_next_token_three_pages():
    """Three-page result set is returned complete."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))

    stubber.add_response(
        "get_query_results",
        _results_resp_with_next_token(["x"], [["1"]], next_token="tok2"),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )
    stubber.add_response(
        "get_query_results",
        _results_resp_page2(["x"], [["2"]], next_token="tok3"),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000, "NextToken": "tok2"},
    )
    stubber.add_response(
        "get_query_results",
        _results_resp_page2(["x"], [["3"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000, "NextToken": "tok3"},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    assert rows == [{"x": "1"}, {"x": "2"}, {"x": "3"}]
    stubber.assert_no_pending_responses()


def test_run_query_header_stripped_once_across_pages():
    """The header row must not appear in the returned data regardless of page count.

    Regression guard: a naive implementation that strips row[0] from every page
    would lose a real data row on each subsequent page.
    """
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))

    # Page 1: header ("name") + 1 data row ("alice")
    stubber.add_response(
        "get_query_results",
        _results_resp_with_next_token(["name"], [["alice"]], next_token="page2"),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )
    # Page 2: 1 data row ("bob") — no header; a buggy impl would treat "bob" as header
    stubber.add_response(
        "get_query_results",
        _results_resp_page2(["name"], [["bob"]]),
        expected_params={
            "QueryExecutionId": EXECUTION_ID,
            "MaxResults": 1000,
            "NextToken": "page2",
        },
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
        )

    names = [r["name"] for r in rows]
    assert "alice" in names, "first data row must be present"
    assert "bob" in names, "second-page data row must be present"
    assert len(rows) == 2, "exactly two data rows expected"


# ---------------------------------------------------------------------------
# AthenaResultTooLargeError — never return truncated results (T6a.1)
# ---------------------------------------------------------------------------


def test_run_query_raises_when_max_rows_exceeded_on_first_page():
    """max_rows=2 with 3 data rows on the first page raises AthenaResultTooLargeError."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(["col_a"], [["r1"], ["r2"], ["r3"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        with pytest.raises(AthenaResultTooLargeError):
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                poll_interval=0,
                max_rows=2,
            )


def test_run_query_raises_when_max_rows_exceeded_across_pages():
    """max_rows=3 with 2 rows on page 1 and 2 rows on page 2 raises AthenaResultTooLargeError."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))

    # Page 1: 2 data rows
    stubber.add_response(
        "get_query_results",
        _results_resp_with_next_token(["col_a"], [["r1"], ["r2"]], next_token="page2"),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )
    # Page 2: 2 more data rows → total 4 > max_rows=3
    stubber.add_response(
        "get_query_results",
        _results_resp_page2(["col_a"], [["r3"], ["r4"]]),
        expected_params={
            "QueryExecutionId": EXECUTION_ID,
            "MaxResults": 1000,
            "NextToken": "page2",
        },
    )

    with stubber:
        with pytest.raises(AthenaResultTooLargeError):
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                poll_interval=0,
                max_rows=3,
            )


def test_run_query_never_returns_partial_list_on_max_rows_breach():
    """AthenaResultTooLargeError is raised; no partial list is returned.

    Specifically: when max_rows is breached, the function must raise — it must
    NOT return the rows accumulated so far.  A truncated list wearing a complete
    face would silently corrupt inputs_hash and break M6 idempotency.
    """
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(["col_a"], [["r1"], ["r2"], ["r3"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    result_holder = []
    with stubber:
        try:
            result_holder.append(
                run_query(
                    SAMPLE_SQL,
                    athena_client=client,
                    workgroup=WORKGROUP,
                    results_s3_path=RESULTS_PATH,
                    poll_interval=0,
                    max_rows=2,
                )
            )
        except AthenaResultTooLargeError:
            pass

    # The function must have raised — result_holder must be empty
    assert result_holder == [], (
        "run_query must raise AthenaResultTooLargeError, not return a partial list"
    )


def test_run_query_at_exact_max_rows_succeeds():
    """Exactly max_rows data rows is accepted (boundary condition)."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(["col_a"], [["r1"], ["r2"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        rows = run_query(
            SAMPLE_SQL,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            poll_interval=0,
            max_rows=2,
        )

    assert len(rows) == 2


def test_run_query_error_message_mentions_max_rows():
    """AthenaResultTooLargeError message states the ceiling for debugging."""
    client, stubber = _athena_client()

    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(["col_a"], [["r1"], ["r2"], ["r3"]]),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        with pytest.raises(AthenaResultTooLargeError) as exc_info:
            run_query(
                SAMPLE_SQL,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                poll_interval=0,
                max_rows=2,
            )

    assert "max_rows=2" in str(exc_info.value)
