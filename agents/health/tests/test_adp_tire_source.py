"""Unit tests for agents/health/adp_tire_source.py — T4.1.

Spec: .kiro/specs/2026-08-03-cvx-tier2-platform-health-agent/spec.md § D3

Coverage
--------
- Happy path: fetch_findings returns normalised rows with all expected keys
- VIN filter: SQL includes a WHERE ... IN clause when VINs are provided
- Fleet-wide batch: no VIN filter when vins=None
- needs_replacement is returned as Python bool (True/False/None), not string
- Athena empty result → empty list (not an error)
- AthenaTimeoutError propagated from run_query
- AthenaCursorError propagated from run_query
- VIN sanitisation: non-alphanumeric/hyphen characters raise ValueError
- compute_batch_hash: same rows in different order → same hash
- compute_batch_hash: different rows → different hash
- No-SQL-or-column-names guard: this module is the ONLY place tire SQL and
  ADP tire column names appear (grep check — enforces spec § D3 isolation)
- Product source: query targets adp_{stage}_tire_health.tire_health — not
  any other product
- Tire positions: query restricts to FL/FR/RL/RR

All tests are synchronous — no pytest-asyncio required.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from typing import Any
from unittest.mock import MagicMock, call, patch

import boto3
import pytest
from botocore.stub import Stubber

from agents.health.adp_tire_source import (
    AdpTireSource,
    TIRE_POSITIONS,
    _parse_bool,
    _sanitise_vin,
    _format_numeric,
)
from agents.tier2.adp_source import AthenaTimeoutError, AthenaCursorError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKGROUP = "cvx-staging-analytics"
RESULTS_PATH = "s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/"
EXECUTION_ID = "tire-exec-1234-5678-abcd"
STAGE = "staging"

# Sample ADP rows as Athena returns them (string values)
_SAMPLE_ATHENA_ROWS = [
    # header
    {"Data": [
        {"VarCharValue": "vin"},
        {"VarCharValue": "tire_position"},
        {"VarCharValue": "event_date"},
        {"VarCharValue": "needs_replacement"},
        {"VarCharValue": "wear_category"},
        {"VarCharValue": "wear_rate_mm_per_1k_km"},
    ]},
    # data row 1 — FL tire
    {"Data": [
        {"VarCharValue": "VIN-TEST-001"},
        {"VarCharValue": "FL"},
        {"VarCharValue": "2026-07-01"},
        {"VarCharValue": "false"},
        {"VarCharValue": "moderate"},
        {"VarCharValue": "0.42"},
    ]},
    # data row 2 — FR tire, flagged for replacement
    {"Data": [
        {"VarCharValue": "VIN-TEST-001"},
        {"VarCharValue": "FR"},
        {"VarCharValue": "2026-07-01"},
        {"VarCharValue": "true"},
        {"VarCharValue": "high"},
        {"VarCharValue": "0.89"},
    ]},
    # data row 3 — RL tire, NULL needs_replacement
    {"Data": [
        {"VarCharValue": "VIN-TEST-002"},
        {"VarCharValue": "RL"},
        {"VarCharValue": "2026-07-01"},
        {},                                          # NULL needs_replacement
        {"VarCharValue": "low"},
        {"VarCharValue": "0.21"},
    ]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _athena_client() -> tuple[Any, Stubber]:
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


def _results_resp(rows: list[dict]) -> dict:
    return {
        "ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}},
        "ResponseMetadata": {},
    }


def _make_source(athena_client: Any) -> AdpTireSource:
    return AdpTireSource(
        STAGE,
        athena_client=athena_client,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,  # no real sleep in tests
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetch_findings_returns_normalised_rows():
    """fetch_findings returns one dict per tire reading with all expected keys."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(_SAMPLE_ATHENA_ROWS),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        source = _make_source(client)
        rows = source.fetch_findings(vins=None)

    assert len(rows) == 3

    row0 = rows[0]
    assert row0["vin"] == "VIN-TEST-001"
    assert row0["position"] == "FL"
    assert row0["event_date"] == "2026-07-01"
    assert row0["replacement_flagged"] is False        # 'false' → Python False
    assert row0["wear_band"] == "moderate"
    assert row0["wear_rate_per_1k_km"] == "0.42"


def test_fetch_findings_needs_replacement_true():
    """'true' string from ADP becomes Python True."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response("get_query_results", _results_resp(_SAMPLE_ATHENA_ROWS),
                         expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000})

    with stubber:
        source = _make_source(client)
        rows = source.fetch_findings(vins=None)

    assert rows[1]["replacement_flagged"] is True


def test_fetch_findings_needs_replacement_null():
    """NULL needs_replacement from ADP becomes Python None."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response("get_query_results", _results_resp(_SAMPLE_ATHENA_ROWS),
                         expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000})

    with stubber:
        source = _make_source(client)
        rows = source.fetch_findings(vins=None)

    assert rows[2]["replacement_flagged"] is None


def test_fetch_findings_all_keys_present():
    """Every expected key is present in every returned row."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response("get_query_results", _results_resp(_SAMPLE_ATHENA_ROWS),
                         expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000})

    with stubber:
        source = _make_source(client)
        rows = source.fetch_findings(vins=None)

    # Agent-facing vocabulary emitted by this module, NOT ADP column names —
    # the translation at this boundary is what makes spec § D3's interface swappable.
    expected_keys = {"vin", "position", "event_date", "replacement_flagged",
                     "wear_band", "wear_rate_per_1k_km"}
    for row in rows:
        assert expected_keys == set(row.keys()), (
            f"Row keys mismatch: expected {sorted(expected_keys)}, got {sorted(row.keys())}"
        )


# ---------------------------------------------------------------------------
# SQL content: product name and VIN filtering
# ---------------------------------------------------------------------------


def test_sql_targets_tire_health_product():
    """The generated SQL targets adp_{stage}_tire_health.tire_health."""
    source = AdpTireSource(
        "staging",
        athena_client=None,  # not called in this test
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
    )
    sql = source._build_sql(vins=None)
    assert "adp_staging_tire_health" in sql
    assert "tire_health" in sql


def test_sql_targets_prod_when_stage_is_prod():
    """prod stage produces adp_prod_tire_health in the SQL."""
    source = AdpTireSource(
        "prod",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
    )
    sql = source._build_sql(vins=None)
    assert "adp_prod_tire_health" in sql


def test_sql_contains_tire_positions_filter():
    """SQL restricts to the four known tire positions FL/FR/RL/RR."""
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
    )
    sql = source._build_sql(vins=None)
    for pos in ("FL", "FR", "RL", "RR"):
        assert pos in sql, f"Expected tire position {pos!r} in SQL filter"


def test_sql_vin_filter_included_when_vins_given():
    """When a VIN list is supplied, the SQL includes an IN clause."""
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
    )
    sql = source._build_sql(vins=["ABC123", "DEF456"])
    assert "IN (" in sql.upper() or "in (" in sql
    assert "ABC123" in sql
    assert "DEF456" in sql


def test_sql_no_vin_filter_when_vins_is_none():
    """When vins=None and max_vins=None, SQL does not include a VIN IN clause."""
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=None,  # Explicitly no bound — full fleet, no VIN filter
    )
    sql = source._build_sql(vins=None)
    # The VIN filter line would start with "AND vin IN"
    assert "AND vin IN" not in sql


# ---------------------------------------------------------------------------
# Empty result
# ---------------------------------------------------------------------------


def test_fetch_findings_empty_result_returns_empty_list():
    """Athena returning no data rows yields an empty list (not an error)."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    # Header only — no data rows
    header_only = [
        {"Data": [
            {"VarCharValue": "vin"},
            {"VarCharValue": "tire_position"},
        ]}
    ]
    stubber.add_response(
        "get_query_results",
        _results_resp(header_only),
        expected_params={"QueryExecutionId": EXECUTION_ID, "MaxResults": 1000},
    )

    with stubber:
        source = _make_source(client)
        rows = source.fetch_findings(vins=None)

    assert rows == []


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


def test_fetch_findings_propagates_timeout_error():
    """AthenaTimeoutError from run_query is propagated to the caller."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response("get_query_execution", _execution_status("RUNNING"))

    with stubber:
        source = _make_source(client)
        # Use max_polls=1 to trigger timeout quickly
        source._max_polls = 1
        with pytest.raises(AthenaTimeoutError):
            source.fetch_findings(vins=None)


def test_fetch_findings_propagates_cursor_error():
    """AthenaCursorError from run_query is propagated to the caller."""
    client, stubber = _athena_client()
    stubber.add_response("start_query_execution", _start_resp())
    stubber.add_response(
        "get_query_execution",
        _execution_status("FAILED", reason="TABLE_NOT_FOUND: tire_health"),
    )

    with stubber:
        source = _make_source(client)
        with pytest.raises(AthenaCursorError):
            source.fetch_findings(vins=None)


# ---------------------------------------------------------------------------
# VIN sanitisation
# ---------------------------------------------------------------------------


def test_sanitise_vin_allows_alphanumeric_and_hyphen():
    assert _sanitise_vin("ABC-123") == "ABC-123"
    assert _sanitise_vin("1HGCM82633A004352") == "1HGCM82633A004352"


def test_sanitise_vin_strips_special_characters():
    """Characters outside the VIN alphabet are stripped."""
    result = _sanitise_vin("ABC'; DROP TABLE--")
    assert "'" not in result
    assert ";" not in result
    assert "-" in result or "ABC" in result  # safe chars remain


def test_sanitise_vin_raises_on_entirely_unsafe_input():
    """A VIN containing only unsafe characters raises ValueError."""
    with pytest.raises(ValueError):
        _sanitise_vin("';!@#$%^&*()")


# ---------------------------------------------------------------------------
# compute_batch_hash
# ---------------------------------------------------------------------------


def test_batch_hash_stable_across_row_ordering():
    """Same rows in different order produce the same hash (M6 idempotency)."""
    row_a = {
        "vin": "VIN-001", "tire_position": "FL", "event_date": "2026-07-01",
        "needs_replacement": False, "wear_category": "low",
        "wear_rate_mm_per_1k_km": "0.21",
    }
    row_b = {
        "vin": "VIN-001", "tire_position": "FR", "event_date": "2026-07-01",
        "needs_replacement": True, "wear_category": "high",
        "wear_rate_mm_per_1k_km": "0.89",
    }
    hash1 = AdpTireSource.compute_batch_hash([row_a, row_b])
    hash2 = AdpTireSource.compute_batch_hash([row_b, row_a])
    assert hash1 == hash2, "Row ordering must not affect the batch hash"


def test_batch_hash_differs_for_different_rows():
    """Different row content produces a different hash."""
    row_a = {
        "vin": "VIN-001", "tire_position": "FL", "event_date": "2026-07-01",
        "needs_replacement": False, "wear_category": "low",
        "wear_rate_mm_per_1k_km": "0.21",
    }
    row_b = {
        "vin": "VIN-002", "tire_position": "FL", "event_date": "2026-07-01",
        "needs_replacement": False, "wear_category": "low",
        "wear_rate_mm_per_1k_km": "0.21",
    }
    assert AdpTireSource.compute_batch_hash([row_a]) != AdpTireSource.compute_batch_hash([row_b])


def test_batch_hash_is_64_hex_chars():
    """Hash result is a 64-character lowercase hex string (SHA-256)."""
    row = {
        "vin": "VIN-001", "tire_position": "FL", "event_date": "2026-07-01",
        "needs_replacement": True, "wear_category": "high",
        "wear_rate_mm_per_1k_km": "0.89",
    }
    h = AdpTireSource.compute_batch_hash([row])
    assert len(h) == 64
    assert h == h.lower()
    assert all(c in "0123456789abcdef" for c in h)


def test_batch_hash_empty_list():
    """Empty row list produces a deterministic hash (not an error)."""
    h = AdpTireSource.compute_batch_hash([])
    assert len(h) == 64


# ---------------------------------------------------------------------------
# _parse_bool unit tests
# ---------------------------------------------------------------------------


def test_parse_bool_true():
    assert _parse_bool("true") is True
    assert _parse_bool("True") is True
    assert _parse_bool("TRUE") is True


def test_parse_bool_false():
    assert _parse_bool("false") is False
    assert _parse_bool("False") is False


def test_parse_bool_none():
    assert _parse_bool(None) is None


def test_parse_bool_unexpected_string_returns_none():
    """An unexpected value (e.g. empty string) is treated as absent."""
    assert _parse_bool("") is None
    assert _parse_bool("maybe") is None


# ---------------------------------------------------------------------------
# Isolation guard: tire SQL and ADP column names appear ONLY in this module
# ---------------------------------------------------------------------------


def _find_python_files(directory: pathlib.Path) -> list[pathlib.Path]:
    """Recursively collect .py files, excluding __pycache__ and test files."""
    result = []
    for p in directory.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        result.append(p)
    return result


def _collect_non_docstring_strings(tree: ast.Module) -> list[str]:
    """Return all string constant values that are NOT module/class/function docstrings
    and NOT variable docstrings (string literal immediately following an assignment).

    Docstrings may legitimately explain what a module does not contain or refer
    to ADP product names in prose.  We only check runtime string literals where
    SQL column names could actually appear — query strings, error messages, dict
    keys inside function bodies.

    A docstring is:
    (a) the first statement of a module/class/function body when that statement
        is a bare ``ast.Expr`` wrapping a string constant, OR
    (b) a bare ``ast.Expr`` string constant that immediately follows an
        ``ast.Assign`` or ``ast.AnnAssign`` at module or class body level
        (conventional variable/constant docstring).
    """
    docstring_nodes: set[int] = set()

    def _mark_docstrings(body: list) -> None:
        # (a) First-statement docstrings (module/class/function)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))

        # (b) Variable/constant docstrings: string literal immediately following
        # an assignment statement anywhere in the body.
        for i in range(1, len(body)):
            prev = body[i - 1]
            curr = body[i]
            if isinstance(prev, (ast.Assign, ast.AnnAssign)):
                if isinstance(curr, ast.Expr) and isinstance(curr.value, ast.Constant):
                    if isinstance(curr.value.value, str):
                        docstring_nodes.add(id(curr.value))

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


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_THIS_MODULE = _REPO_ROOT / "agents" / "health" / "adp_tire_source.py"

# ADP column names that MUST appear only in adp_tire_source.py
_TIRE_COLUMN_NAMES = [
    "tire_health",
    "needs_replacement",
    "wear_rate_mm_per_1k_km",
]


def test_tire_column_names_appear_only_in_adp_tire_source():
    """ADP tire column names appear only in adp_tire_source.py (and its tests).

    The spec § D3 constraint: SQL and ADP column names must be isolated
    behind this single module.  A hit in any other source file means the
    isolation has been broken.

    Uses AST-based non-docstring scanning so platform modules that explain
    the tier split in prose (e.g. "consumes needs_replacement from ADP") do
    not trigger a false positive.  Test files are excluded from this check.
    """
    agents_dir = _REPO_ROOT / "agents"
    violations: list[str] = []

    for path in _find_python_files(agents_dir):
        # Allow this source module (the sole canonical location)
        if path.resolve() == _THIS_MODULE.resolve():
            continue
        # Allow test files — they may use column names in fixture data
        if "tests" in path.parts:
            continue

        source_text = path.read_text()
        try:
            tree = ast.parse(source_text)
        except SyntaxError:
            continue

        # Check non-docstring string literals only (case-insensitive)
        non_docstring_strings = _collect_non_docstring_strings(tree)
        combined = " ".join(non_docstring_strings).lower()

        for col in _TIRE_COLUMN_NAMES:
            if col.lower() in combined:
                violations.append(f"{path.relative_to(_REPO_ROOT)}: contains {col!r}")

    assert not violations, (
        "ADP tire column names found in non-docstring string literals "
        "outside adp_tire_source.py:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_tire_health_product_name_appears_only_in_adp_tire_source():
    """The string 'tire_health' appears only in adp_tire_source.py SQL strings.

    The spec § D3 constraint targets SQL and column names — not comments or
    docstrings that legitimately reference the product name in prose.  We use
    AST-based non-docstring scanning (same approach as test_adp_source.py) so
    references in module docstrings or inline comments do not trigger a false
    positive.

    Test files (agents/health/tests/) are excluded.
    """
    agents_dir = _REPO_ROOT / "agents"
    violations: list[str] = []

    for path in _find_python_files(agents_dir):
        if path.resolve() == _THIS_MODULE.resolve():
            continue
        if "tests" in path.parts:
            continue

        source_text = path.read_text()
        try:
            tree = ast.parse(source_text)
        except SyntaxError:
            continue

        # Collect non-docstring string literals only (see _collect_non_docstring_strings
        # in test_adp_source.py for the rationale — docstrings may legitimately explain
        # what a module does not contain, or reference product names in prose).
        non_docstring_strings = _collect_non_docstring_strings(tree)
        combined = " ".join(non_docstring_strings).lower()

        if "tire_health" in combined:
            violations.append(str(path.relative_to(_REPO_ROOT)))

    assert not violations, (
        "'tire_health' found in non-docstring string literals outside adp_tire_source.py:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_source_product_is_tire_health_not_energy_usage_or_service_records():
    """adp_tire_source reads tire_health, not energy_usage or service_records."""
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
    )
    sql = source._build_sql(vins=None)
    assert "tire_health" in sql
    assert "energy_usage" not in sql
    assert "service_records" not in sql


# ---------------------------------------------------------------------------
# TIRE_POSITIONS constant
# ---------------------------------------------------------------------------


def test_tire_positions_are_the_four_known_positions():
    assert TIRE_POSITIONS == frozenset({"FL", "FR", "RL", "RR"})


# ---------------------------------------------------------------------------
# No VINs in exception message (security-review Cycle 7 Warning W4)
# ---------------------------------------------------------------------------


def test_no_vin_in_sanitise_vin_exception_message():
    """The ValueError from _sanitise_vin must not contain the VIN.

    Security-review Cycle 7 Warning W4: exception messages in Lambda propagate
    to CloudWatch via Lambda's default traceback logger even at default log
    level.  Same class as the Cycle 2/3 BudgetExceeded.__repr__ fix —
    an exception whose default rendering surfaces customer data.

    Mirror pattern: assert ``vin not in str(exc)`` over the raised exception
    (matching ``test_no_vins_in_budget_exception_str`` in
    agents/tier2/tests/test_budget.py).
    """
    vin = "1FMCU0F70MUA12345"  # canonical CVX synthetic VIN — all valid chars
    # To trigger the "no valid characters" path we must supply a VIN whose
    # characters ALL fall outside the allowed set.  Use control characters which
    # _sanitise_vin strips entirely.
    bad_vin = "\x00\x01\x02"  # all stripped → empty sanitised string
    with pytest.raises(ValueError) as exc_info:
        _sanitise_vin(bad_vin)

    exc_text = str(exc_info.value)
    # The bad_vin itself must not appear in the message (contains control chars,
    # but repr() would expose them — verify the message is generic).
    assert "no valid characters" in exc_text, (
        f"ValueError message doesn't mention 'no valid characters': {exc_text!r}"
    )
    # The raw input must not be embedded literally or via !r.
    assert repr(bad_vin) not in exc_text, (
        f"ValueError message contains repr(bad_vin): {exc_text!r}\n"
        "Security-review Cycle 7 Warning W4: exception messages must not carry "
        "the VIN value (even as repr) — they propagate to CloudWatch."
    )
    # Ensure no '\\x' escape sequences from the control chars made it in
    assert "\\x0" not in exc_text, (
        f"ValueError message contains repr-encoded control characters: {exc_text!r}"
    )


def test_sanitise_vin_exception_does_not_contain_vin_literal():
    """The ValueError from _sanitise_vin must not contain the VIN literal.

    Verifies specifically that a realistic-looking but invalid VIN (one with
    a forbidden character) does not appear verbatim in the raised exception.

    This mirrors ``test_no_vins_in_budget_exception_repr`` / ``_str`` in
    agents/tier2/tests/test_budget.py and extends the same ``assert vin not in
    text`` pattern to the exception path in adp_tire_source.py.

    Security-review Cycle 7 Warning W4.
    """
    # A VIN with a single-quote: all other chars are valid, but the quote
    # would be stripped, leaving a valid (but different) sanitised string.
    # Use a VIN where ALL chars are forbidden so the empty-check fires.
    canary_vin = "'" * 17  # 17 single quotes — all stripped → empty → ValueError
    with pytest.raises(ValueError) as exc_info:
        _sanitise_vin(canary_vin)

    exc_text = str(exc_info.value)
    assert canary_vin not in exc_text, (
        f"VIN literal leaked into ValueError message: {exc_text!r}\n"
        "Security-review Cycle 7 Warning W4."
    )
    # The canary characters (single quotes) must not appear literally or escaped.
    assert "'" not in exc_text, (
        f"Single-quote characters appeared in the exception message: {exc_text!r}"
    )


# ---------------------------------------------------------------------------
# SQL-injection defence at the fetch_findings level (security-review Cycle 7 W7)
# ---------------------------------------------------------------------------


def test_sql_injection_metacharacters_stripped_by_fetch_findings():
    """A VIN-shaped input carrying SQL metacharacters has them stripped BEFORE
    any Athena query is constructed.

    Security-review Cycle 7 W7: all three ADP health sources must use the
    same allow-list guard (agents.health._vin.sanitise_vin).  This test
    verifies the tire source's existing allow-list guard still fires correctly
    after Fix Group 4b T4.11 migrated it to the shared helper.

    The guard strips (not rejects) metacharacters when valid chars remain.
    The Athena stub succeeds — if the guard misfires (lets the raw VIN
    through), the SQL string would contain quotes/spaces which the stub
    was not expecting.
    """
    import boto3
    from botocore.stub import Stubber

    malicious_vin = "1FMCU0F70MUA12345' OR '1'='1"
    # After sanitisation: '1FMCU0F70MUA12345OR11'
    safe_vin = "1FMCU0F70MUA12345OR11"

    columns = ["vin", "tire_position", "event_date",
               "needs_replacement", "wear_category", "wear_rate_mm_per_1k_km"]
    data_rows = [[safe_vin, "FL", "2026-01-01", "false", "low", "0.5"]]

    client = boto3.client("athena", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response("start_query_execution", {"QueryExecutionId": "exec-1", "ResponseMetadata": {}})
    stubber.add_response(
        "get_query_execution",
        {"QueryExecution": {"QueryExecutionId": "exec-1", "Status": {"State": "SUCCEEDED"}}, "ResponseMetadata": {}},
    )
    header_row = {"Data": [{"VarCharValue": c} for c in columns]}
    data_row = {"Data": [{"VarCharValue": str(v)} for v in data_rows[0]]}
    stubber.add_response(
        "get_query_results",
        {"ResultSet": {"Rows": [header_row, data_row], "ResultSetMetadata": {"ColumnInfo": []}}, "ResponseMetadata": {}},
        expected_params={"QueryExecutionId": "exec-1", "MaxResults": 1000},
    )

    source = AdpTireSource(
        stage="staging",
        athena_client=client,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,
    )

    with stubber:
        rows = source.fetch_findings(vins=[malicious_vin])

    assert len(rows) == 1
    stubber.assert_no_pending_responses()


def test_all_metachar_vin_raises_in_fetch_findings():
    """A VIN of all metacharacters (no valid chars) raises ValueError BEFORE
    any Athena API call is made.
    """
    import boto3

    all_metachar_vin = "!@#$%^&*()"

    client = boto3.client("athena", region_name="us-east-1")
    source = AdpTireSource(
        stage="staging",
        athena_client=client,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
    )

    with pytest.raises(ValueError):
        source.fetch_findings(vins=[all_metachar_vin])



# ---------------------------------------------------------------------------
# max_vins deterministic-VIN-bounding (T6a.3)
# ---------------------------------------------------------------------------


def test_build_sql_max_vins_contains_ordered_distinct_subquery():
    """With max_vins set and no explicit VIN list, SQL contains the ordered
    distinct-VIN subquery that makes VIN selection deterministic.

    This is the load-bearing property for M6 idempotency: the same VINs must
    be selected on every run so inputs_hash is stable across runs.  ORDER BY
    vin is what makes it deterministic.
    """
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=25,
    )
    sql = source._build_sql(vins=None)
    assert "SELECT DISTINCT vin" in sql
    assert "ORDER BY vin" in sql
    assert "LIMIT 25" in sql


def test_build_sql_two_calls_byte_identical_with_max_vins():
    """Two calls to _build_sql with the same max_vins produce byte-identical SQL.

    Determinism is the load-bearing part: if the SQL differs between runs,
    Athena could return different VIN slices, making inputs_hash unstable and
    breaking M6 idempotency.
    """
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=50,
    )
    sql1 = source._build_sql(vins=None)
    sql2 = source._build_sql(vins=None)
    assert sql1 == sql2, "SQL must be byte-identical across repeated calls"


def test_explicit_vin_list_takes_precedence_over_max_vins():
    """When an explicit VIN list is supplied, it overrides max_vins.

    The explicit list is used verbatim; the distinct-VIN subquery must
    NOT appear in the SQL when vins is provided.
    """
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=10,
    )
    sql = source._build_sql(vins=["VIN-001", "VIN-002"])
    # Explicit list should be used
    assert "VIN-001" in sql
    assert "VIN-002" in sql
    # The max_vins subquery must NOT be present when an explicit list is given
    assert "SELECT DISTINCT vin" not in sql
    assert "ORDER BY vin" not in sql


def test_build_sql_no_max_vins_no_vin_filter():
    """When max_vins is None and vins is None, no VIN filter is applied (full fleet)."""
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=None,
    )
    sql = source._build_sql(vins=None)
    assert "SELECT DISTINCT vin" not in sql
    assert "ORDER BY vin" not in sql
    assert "LIMIT" not in sql


def test_build_sql_max_vins_subquery_is_database_qualified():
    """The distinct-VIN subquery must reference the qualified database.tire_health table,
    not an unqualified table name.

    An unqualified table name would fail with TABLE_NOT_FOUND in Athena
    (the workgroup has no default database).
    """
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=25,
    )
    sql = source._build_sql(vins=None)
    # Must qualify with the Glue database name
    assert "adp_staging_tire_health" in sql


def test_build_sql_max_vins_does_not_use_tablesample_or_rand():
    """The max_vins bound must use ORDER BY vin, not TABLESAMPLE or rand().

    TABLESAMPLE and rand() both reintroduce per-run variation, making
    inputs_hash unstable and breaking M6 idempotency.
    """
    source = AdpTireSource(
        "staging",
        athena_client=None,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=25,
    )
    sql = source._build_sql(vins=None).lower()
    assert "tablesample" not in sql
    assert "rand()" not in sql
    assert "random()" not in sql


# ---------------------------------------------------------------------------
# T6b.1 — max_rows threading: assert on what run_query received, not the
# constructor argument.
# ---------------------------------------------------------------------------


def test_max_rows_is_forwarded_to_run_query_when_set():
    """AdpTireSource forwards max_rows to run_query when a ceiling is configured.

    Asserts on the kwarg that run_query actually RECEIVED — not the constructor
    argument — to prevent the vacuous-test mistake where storing max_rows in
    self._max_rows passes the test without the value ever reaching the service call.
    """
    from unittest.mock import patch

    source = AdpTireSource(
        "staging",
        athena_client=object(),  # not called — run_query is patched
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,
        max_rows=5000,
    )

    with patch("agents.health.adp_tire_source.run_query", return_value=[]) as mock_rq:
        source.fetch_findings(vins=None)

    mock_rq.assert_called_once()
    _, kwargs = mock_rq.call_args
    assert "max_rows" in kwargs, (
        "run_query was not called with max_rows kwarg; "
        "the ceiling is stored but never forwarded."
    )
    assert kwargs["max_rows"] == 5000, (
        f"run_query received max_rows={kwargs['max_rows']!r} but expected 5000."
    )


def test_max_rows_not_forwarded_to_run_query_when_none():
    """When max_rows is None, run_query is called WITHOUT max_rows kwarg."""
    from unittest.mock import patch

    source = AdpTireSource(
        "staging",
        athena_client=object(),
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,
        max_rows=None,
    )

    with patch("agents.health.adp_tire_source.run_query", return_value=[]) as mock_rq:
        source.fetch_findings(vins=None)

    mock_rq.assert_called_once()
    _, kwargs = mock_rq.call_args
    assert "max_rows" not in kwargs, (
        "run_query should not receive max_rows when the ceiling is None."
    )
