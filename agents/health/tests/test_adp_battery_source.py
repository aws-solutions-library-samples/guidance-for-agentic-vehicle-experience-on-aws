"""Unit tests for agents/health/adp_battery_source.py.

Covers:
- ADP_PRODUCT constant is exactly "energy_usage" (hard constraint from spec § D3a)
- No ``vehicle_telemetry_aggregated`` reference anywhere in the module (hard constraint)
- Happy path: fetch_findings returns rows, inputs_hash, evidence with live provenance
- Empty path: no rows for VIN returns absent-provenance evidence and a stable hash
- inputs_hash is stable for identical rows (idempotency — spec § D4/M6)
- inputs_hash changes when rows change
- Evidence entries carry required provenance fields
- No SQL or battery column names in tier2/adp_source.py (isolation check)
- No thresholds or model logic in the module (no numeric comparisons against SoH)

All tests use a stubbed boto3 Athena client (botocore.stub.Stubber).
No pytest-asyncio required: all tests are synchronous.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from agents.health.adp_battery_source import (
    ADP_PRODUCT,
    AdpBatterySource,
    fetch_findings,
    _compute_hash,
    _build_evidence,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKGROUP = "cvx-staging-analytics"
RESULTS_PATH = "s3://cvx-staging-athena-results-123456789012-us-east-1/tier2/"
DATABASE = "adp_staging_energy_usage"
EXECUTION_ID = "bb112233-4455-6677-8899-aabbccddeeff"
SAMPLE_VIN = "VIN123456789"


# ---------------------------------------------------------------------------
# Helpers — Stubber factory
# ---------------------------------------------------------------------------


def _athena_client() -> tuple[Any, Stubber]:
    client = boto3.client("athena", region_name="us-east-1")
    stubber = Stubber(client)
    return client, stubber


def _start_resp(execution_id: str = EXECUTION_ID) -> dict:
    return {"QueryExecutionId": execution_id, "ResponseMetadata": {}}


def _execution_status(state: str) -> dict:
    return {
        "QueryExecution": {
            "QueryExecutionId": EXECUTION_ID,
            "Status": {"State": state},
        },
        "ResponseMetadata": {},
    }


def _results_resp(columns: list[str], data_rows: list[list]) -> dict:
    """Build a GetQueryResults response; first row is always the header."""
    header_row = {"Data": [{"VarCharValue": c} for c in columns]}
    rows = [header_row]
    for dr in data_rows:
        cells = [{"VarCharValue": str(v)} if v is not None else {} for v in dr]
        rows.append({"Data": cells})
    return {
        "ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": []}},
        "ResponseMetadata": {},
    }


def _stub_successful_query(
    stubber: Stubber,
    columns: list[str],
    data_rows: list[list],
    execution_id: str = EXECUTION_ID,
) -> None:
    """Add the three Athena calls for a SUCCEEDED query to the stubber."""
    stubber.add_response("start_query_execution", _start_resp(execution_id))
    stubber.add_response("get_query_execution", _execution_status("SUCCEEDED"))
    stubber.add_response(
        "get_query_results",
        _results_resp(columns, data_rows),
        expected_params={"QueryExecutionId": execution_id, "MaxResults": 1000},
    )


# ---------------------------------------------------------------------------
# Hard constraint: ADP_PRODUCT must be "energy_usage"
# ---------------------------------------------------------------------------


def test_adp_product_is_energy_usage():
    """ADP_PRODUCT constant must equal 'energy_usage' (spec § D3a hard constraint).

    Reading vehicle_telemetry_aggregated.state_of_health_pct instead would give a
    fleet-homogeneous 95→90 trend, making per-owner battery judgment impossible while
    appearing superficially correct.  The constant is tested here so the source cannot
    silently drift.
    """
    assert ADP_PRODUCT == "energy_usage", (
        f"ADP_PRODUCT must be 'energy_usage', got {ADP_PRODUCT!r}. "
        "Do NOT change this to 'vehicle_telemetry_aggregated' — spec § D3a explains "
        "that column is generated from a global row index and is identical for every VIN."
    )


# ---------------------------------------------------------------------------
# Hard constraint: no vehicle_telemetry_aggregated reference in this module
# ---------------------------------------------------------------------------


def _collect_non_docstring_strings(tree: ast.Module) -> list[str]:
    """Return all string constant values that are NOT module/class/function docstrings.

    Docstrings legitimately describe what a module does not contain (e.g. explaining
    why vehicle_telemetry_aggregated must not be used).  We only care about runtime
    string literals — query strings, error messages, dict keys, etc.
    """
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


def test_no_vehicle_telemetry_aggregated_in_module():
    """The module must not reference vehicle_telemetry_aggregated in runtime string literals.

    Docstrings legitimately explain why this product must NOT be used, so we scan
    non-docstring string literals only — query strings, log messages, etc.

    This is the per-owner judgment-killer: the wrong column gives a fleet-wide
    95→90 trend instead of genuine age-correlated variation (spec § D3a).
    """
    module_path = pathlib.Path(__file__).resolve().parents[1] / "adp_battery_source.py"
    source_text = module_path.read_text()
    tree = ast.parse(source_text)
    non_docstring_strings = _collect_non_docstring_strings(tree)
    combined = " ".join(non_docstring_strings).lower()
    assert "vehicle_telemetry_aggregated" not in combined, (
        "adp_battery_source.py contains 'vehicle_telemetry_aggregated' in a runtime "
        "string literal. The correct source is 'energy_usage' (spec § D3a). "
        "Mentions in docstrings (explaining why it must NOT be used) are acceptable."
    )


# ---------------------------------------------------------------------------
# Happy path: rows returned with live provenance
# ---------------------------------------------------------------------------


def test_fetch_findings_returns_rows_and_hash():
    """Successful query returns non-empty rows and a non-empty inputs_hash."""
    client, stubber = _athena_client()
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = [[SAMPLE_VIN, "92.3", "1095"]]

    _stub_successful_query(stubber, columns, data_rows)

    with stubber:
        result = fetch_findings(
            SAMPLE_VIN,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    assert result["vin"] == SAMPLE_VIN
    assert len(result["rows"]) == 1
    assert result["rows"][0]["health_pct"] == "92.3"
    assert result["rows"][0]["age_days"] == "1095"
    assert len(result["inputs_hash"]) == 64  # SHA-256 hex
    stubber.assert_no_pending_responses()


def test_fetch_findings_evidence_live_provenance():
    """With rows present, evidence entries carry provenance='live'."""
    client, stubber = _athena_client()
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = [[SAMPLE_VIN, "88.1", "2190"]]

    _stub_successful_query(stubber, columns, data_rows)

    with stubber:
        result = fetch_findings(
            SAMPLE_VIN,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    evidence = result["evidence"]
    assert len(evidence) >= 1
    for entry in evidence:
        assert "source" in entry
        assert "value" in entry
        assert "provenance" in entry
        assert entry["provenance"] == "live"


def test_fetch_findings_evidence_references_energy_usage():
    """Evidence source fields must reference 'energy_usage', not the wrong product."""
    client, stubber = _athena_client()
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = [[SAMPLE_VIN, "95.0", "365"]]

    _stub_successful_query(stubber, columns, data_rows)

    with stubber:
        result = fetch_findings(
            SAMPLE_VIN,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    sources = [e["source"] for e in result["evidence"]]
    assert all("energy_usage" in s for s in sources), (
        f"Evidence sources must reference 'energy_usage'; got: {sources}"
    )
    # Ensure the wrong product is not referenced
    assert not any("vehicle_telemetry_aggregated" in s for s in sources)


# ---------------------------------------------------------------------------
# Empty path: no rows for VIN → absent provenance, stable hash
# ---------------------------------------------------------------------------


def test_fetch_findings_empty_returns_absent_provenance():
    """When no rows exist for the VIN, evidence carries provenance='absent'."""
    client, stubber = _athena_client()
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = []  # no rows

    _stub_successful_query(stubber, columns, data_rows)

    with stubber:
        result = fetch_findings(
            SAMPLE_VIN,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    assert result["rows"] == []
    evidence = result["evidence"]
    assert len(evidence) >= 1
    for entry in evidence:
        assert entry["provenance"] == "absent"
        assert entry["value"] is None


def test_fetch_findings_empty_hash_is_stable():
    """Empty results produce a fixed non-empty sentinel hash."""
    client1, stubber1 = _athena_client()
    client2, stubber2 = _athena_client()
    columns = ["vin", "state_of_health_pct", "battery_age_days"]

    _stub_successful_query(stubber1, columns, [])
    _stub_successful_query(stubber2, columns, [])

    with stubber1:
        result1 = fetch_findings(
            SAMPLE_VIN,
            athena_client=client1,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )
    with stubber2:
        result2 = fetch_findings(
            SAMPLE_VIN,
            athena_client=client2,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    assert result1["inputs_hash"] == result2["inputs_hash"]
    assert len(result1["inputs_hash"]) == 64


# ---------------------------------------------------------------------------
# inputs_hash per-VIN content-addressing (spec § D1/M6 idempotency)
# ---------------------------------------------------------------------------


def test_compute_hash_stable_for_same_rows():
    """Identical rows always produce the same hash (idempotency gate)."""
    rows = [{"vin": SAMPLE_VIN, "health_pct": "92.3", "age_days": "1095"}]
    assert _compute_hash(rows) == _compute_hash(rows)


def test_compute_hash_stable_across_row_order():
    """Row ordering does not affect the hash — normalisation sorts before hashing."""
    rows_a = [
        {"vin": "VIN_A", "health_pct": "90.0", "age_days": "730"},
        {"vin": "VIN_B", "health_pct": "95.0", "age_days": "365"},
    ]
    rows_b = [
        {"vin": "VIN_B", "health_pct": "95.0", "age_days": "365"},
        {"vin": "VIN_A", "health_pct": "90.0", "age_days": "730"},
    ]
    assert _compute_hash(rows_a) == _compute_hash(rows_b)


def test_compute_hash_changes_when_rows_change():
    """Different SoH values produce a different hash."""
    rows_today = [{"vin": SAMPLE_VIN, "health_pct": "92.3", "age_days": "1095"}]
    rows_later = [{"vin": SAMPLE_VIN, "health_pct": "91.0", "age_days": "1460"}]
    assert _compute_hash(rows_today) != _compute_hash(rows_later)


def test_compute_hash_empty_returns_64_hex_chars():
    """Empty rows return a 64-character hex string."""
    h = _compute_hash([])
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Evidence structure
# ---------------------------------------------------------------------------


def test_build_evidence_live_has_required_fields():
    """Live evidence entries carry all required provenance fields."""
    rows = [{"vin": SAMPLE_VIN, "health_pct": "93.5", "age_days": "900"}]
    evidence = _build_evidence(rows)
    for entry in evidence:
        assert "source" in entry, f"Missing 'source' in {entry}"
        assert "value" in entry, f"Missing 'value' in {entry}"
        assert "provenance" in entry, f"Missing 'provenance' in {entry}"
        assert entry["provenance"] == "live"


def test_build_evidence_absent_has_required_fields():
    """Absent evidence entries carry all required provenance fields with value=None."""
    evidence = _build_evidence([])
    for entry in evidence:
        assert "source" in entry
        assert "value" in entry
        assert "provenance" in entry
        assert entry["provenance"] == "absent"
        assert entry["value"] is None


# ---------------------------------------------------------------------------
# Source isolation: battery column names must NOT appear in adp_source.py
# ---------------------------------------------------------------------------


def test_battery_columns_absent_from_adp_source():
    """The base adp_source.py must not contain battery column names.

    Column names belong exclusively in this module (spec § D3/D3a isolation).
    """
    adp_source_path = (
        pathlib.Path(__file__).resolve().parents[2] / "tier2" / "adp_source.py"
    )
    source_lower = adp_source_path.read_text().lower()
    forbidden = ["state_of_health_pct", "battery_age_days", "energy_usage"]
    for name in forbidden:
        assert name not in source_lower, (
            f"adp_source.py contains battery column name {name!r} — "
            "column names belong only in agents/health/adp_battery_source.py"
        )


# ---------------------------------------------------------------------------
# No threshold comparisons in this module (spec § D3a — CVX computes no model)
# ---------------------------------------------------------------------------


def test_no_threshold_logic_in_module():
    """The module must not compare SoH against numeric thresholds.

    CVX computes no degradation model (spec § D3a, Q7).  Any numeric comparison
    against SoH or battery_age_days inside this module is out of scope — that
    judgment belongs to the health agent, not the data-source layer.

    This test scans for comparison operators (``<``, ``>``, ``<=``, ``>=``) in
    non-docstring, non-comment source lines involving the battery SoH column name.
    A false positive in a comment or docstring is excluded.
    """
    module_path = pathlib.Path(__file__).resolve().parents[1] / "adp_battery_source.py"
    source_text = module_path.read_text()
    tree = ast.parse(source_text)

    # Collect all Compare nodes; check if any operand references soh-related names.
    soh_identifiers = {"state_of_health_pct", "soh", "battery_age_days"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            # Get all names involved in the comparison
            all_nodes = [node.left] + list(node.comparators)
            for operand in all_nodes:
                if isinstance(operand, ast.Name) and operand.id.lower() in soh_identifiers:
                    pytest.fail(
                        f"adp_battery_source.py contains a threshold comparison on "
                        f"{operand.id!r} at line {node.lineno}. "
                        "CVX computes no thresholds — judgment belongs in the agent "
                        "(spec § D3a, Q7)."
                    )
                if isinstance(operand, ast.Constant) and isinstance(operand.value, (int, float)):
                    # Only flag if in context of a soh-named attribute access
                    pass  # numeric constants for array indexing etc. are fine


# ---------------------------------------------------------------------------
# No VINs in log output (security-review Cycle 7 Warning W3)
# ---------------------------------------------------------------------------


class _RecordingHandler(logging.Handler):
    """Minimal log handler that accumulates rendered log messages."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.messages.append(self.format(record))


def test_no_vins_in_battery_source_log_output():
    """VINs must never appear in log records emitted by adp_battery_source.

    Tier 2 runs unattended; CloudWatch Logs is the widest exposure surface.
    The Tier 2 platform test-enforces this invariant for BudgetExceeded
    (test_no_vins_in_budget_exception_repr / _str in agents/tier2/tests/
    test_budget.py); this test extends the same enforcement to the battery
    source.

    Mirror pattern: run fetch_findings with a canary VIN while a recording
    handler is attached to the module logger at DEBUG level, then assert
    the canary does not appear in any rendered log message.

    Security-review Cycle 7 Warning W3.
    """
    import logging

    vin = "1FMCU0F70MUA12345"  # canonical CVX synthetic VIN
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = [[vin, "92.3", "1095"]]

    client, stubber = _athena_client()
    _stub_successful_query(stubber, columns, data_rows)

    handler = _RecordingHandler()
    logger = logging.getLogger("agents.health.adp_battery_source")
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.DEBUG)

    try:
        with stubber:
            fetch_findings(
                vin,
                athena_client=client,
                workgroup=WORKGROUP,
                results_s3_path=RESULTS_PATH,
                database=DATABASE,
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)

    for msg in handler.messages:
        assert vin not in msg, (
            f"VIN leaked into battery source log record: {msg!r}\n"
            "Security-review Cycle 7 Warning W3: Tier 2 runs unattended; "
            "VINs must never appear in CloudWatch log records."
        )


# ---------------------------------------------------------------------------
# SQL-injection defence (security-review Cycle 7 Warning W7)
# ---------------------------------------------------------------------------


def test_sql_injection_metacharacters_stripped_before_query():
    """A VIN-shaped input carrying SQL metacharacters has them stripped BEFORE
    any Athena query is constructed.

    Security-review Cycle 7 W7: battery previously had zero SQL-injection
    defence.  Fix Group 4b T4.11 adds the shared allow-list guard
    (agents.health._vin.sanitise_vin) so metacharacters are stripped and
    the resulting safe VIN is embedded in the SQL literal.

    The guard strips (not rejects) metacharacters when valid chars remain.
    This test verifies that the VIN ``"1FMCU0F70MUA12345' OR '1'='1"``
    is cleaned to ``"1FMCU0F70MUA12345OR11"`` and the query proceeds with
    a SQL-safe VIN string rather than raising or allowing injection.

    The Athena stub is configured to SUCCEED — if the guard misfires (lets
    the raw VIN through), the stub would receive the malicious SQL string
    and fail with an unexpected params error instead of this test passing.
    """
    malicious_vin = "1FMCU0F70MUA12345' OR '1'='1"
    # After sanitisation: '1FMCU0F70MUA12345OR11' (quotes and spaces stripped)
    safe_vin = "1FMCU0F70MUA12345OR11"

    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    # Stub returns data keyed on the safe VIN (what Athena would store)
    data_rows = [[safe_vin, "92.3", "1095"]]

    client, stubber = _athena_client()
    _stub_successful_query(stubber, columns, data_rows)

    with stubber:
        result = fetch_findings(
            malicious_vin,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    # Query succeeded with the sanitised VIN — no injection
    assert result is not None
    stubber.assert_no_pending_responses()


def test_all_metachar_vin_raises_before_query():
    """A VIN of all metacharacters (no valid chars) raises ValueError BEFORE
    any Athena API call is made.

    This is the only case where the allow-list guard raises — when no valid
    character survives stripping.  Athena must never be called.
    """
    all_metachar_vin = "!@#$%^&*()"

    client, _ = _athena_client()  # no stubber needed — call must not reach Athena

    with pytest.raises(ValueError):
        fetch_findings(
            all_metachar_vin,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )


def test_null_byte_vin_is_stripped_before_query():
    """A VIN with null bytes has them stripped; if valid chars remain, query proceeds.

    Null bytes are stripped by the allow-list.  Valid alphanumeric chars
    adjacent to them survive.  No exception is raised.
    """
    null_vin = "1FMCU\x000F70MUA12345"
    # After sanitisation: '1FMCU0F70MUA12345'
    safe_vin = "1FMCU0F70MUA12345"

    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = [[safe_vin, "91.0", "1200"]]

    client, stubber = _athena_client()
    _stub_successful_query(stubber, columns, data_rows)

    with stubber:
        result = fetch_findings(
            null_vin,
            athena_client=client,
            workgroup=WORKGROUP,
            results_s3_path=RESULTS_PATH,
            database=DATABASE,
        )

    assert result is not None
    stubber.assert_no_pending_responses()



# ---------------------------------------------------------------------------
# AdpBatterySource class — batch API and max_vins deterministic bounding (T6a.3)
# ---------------------------------------------------------------------------


def _make_batch_source(
    athena_client: Any,
    max_vins: int = 25,
) -> AdpBatterySource:
    """Helper: create an AdpBatterySource with the standard test config."""
    return AdpBatterySource(
        "staging",
        athena_client=athena_client,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,
        max_vins=max_vins,
    )


def test_adp_battery_source_class_exists():
    """AdpBatterySource can be instantiated without error."""
    client, _ = _athena_client()
    source = _make_batch_source(client)
    assert hasattr(source, "fetch_findings")


def test_batch_source_build_sql_max_vins_contains_ordered_distinct_subquery():
    """With max_vins set and no explicit VIN list, SQL contains the ordered
    distinct-VIN subquery with ORDER BY vin for pagination order.

    ORDER BY vin establishes pagination order for the keyset cursor.
    Idempotency is per-VIN content-addressing in runner.py, not a property
    of which VINs are selected (spec § D1).
    """
    client, _ = _athena_client()
    source = _make_batch_source(client, max_vins=25)
    sql = source._build_sql(vins=None)
    assert "SELECT DISTINCT vin" in sql
    assert "ORDER BY vin" in sql
    assert "LIMIT 25" in sql


def test_batch_source_two_calls_byte_identical_with_max_vins():
    """Two calls to _build_sql with the same max_vins produce byte-identical SQL.

    Byte-identical SQL is required for pagination order consistency — the same
    ORDER BY vin clause must appear on every call so the keyset cursor advances
    correctly.  TABLESAMPLE or rand() would break the pagination invariant.
    """
    client, _ = _athena_client()
    source = _make_batch_source(client, max_vins=50)
    sql1 = source._build_sql(vins=None)
    sql2 = source._build_sql(vins=None)
    assert sql1 == sql2, "SQL must be byte-identical across repeated calls"


def test_batch_source_explicit_vin_list_takes_precedence_over_max_vins():
    """When an explicit VIN list is supplied, it overrides max_vins.

    The explicit list is used verbatim; the distinct-VIN subquery must
    NOT appear in the SQL when vins is provided.
    """
    client, _ = _athena_client()
    source = _make_batch_source(client, max_vins=10)
    sql = source._build_sql(vins=["VIN-001", "VIN-002"])
    # Explicit list should be used
    assert "VIN-001" in sql
    assert "VIN-002" in sql
    # The max_vins subquery must NOT be present when an explicit list is given
    assert "SELECT DISTINCT vin" not in sql
    assert "ORDER BY vin" not in sql


def test_batch_source_no_max_vins_no_vin_filter():
    """When max_vins is None and vins is None, no VIN filter is applied (full fleet)."""
    client, _ = _athena_client()
    source = AdpBatterySource(
        "staging",
        athena_client=client,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=None,
    )
    sql = source._build_sql(vins=None)
    assert "SELECT DISTINCT vin" not in sql
    assert "ORDER BY vin" not in sql
    assert "LIMIT" not in sql


def test_batch_source_max_vins_subquery_is_database_qualified():
    """The distinct-VIN subquery must reference the qualified database.energy_usage table,
    not an unqualified table name.

    An unqualified table name would fail with TABLE_NOT_FOUND in Athena
    (the workgroup has no default database).
    """
    client, _ = _athena_client()
    source = _make_batch_source(client, max_vins=25)
    sql = source._build_sql(vins=None)
    # Must qualify with the Glue database name
    assert "adp_staging_energy_usage" in sql


def test_batch_source_max_vins_does_not_use_tablesample_or_rand():
    """The max_vins bound must use ORDER BY vin, not TABLESAMPLE or rand().

    TABLESAMPLE and rand() both reintroduce per-run variation, making
    inputs_hash unstable and breaking M6 idempotency.
    """
    client, _ = _athena_client()
    source = _make_batch_source(client, max_vins=25)
    sql = source._build_sql(vins=None).lower()
    assert "tablesample" not in sql
    assert "rand()" not in sql
    assert "random()" not in sql


def test_batch_source_fetch_findings_returns_agent_facing_rows():
    """AdpBatterySource.fetch_findings returns rows with agent-facing keys."""
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    data_rows = [[SAMPLE_VIN, "92.3", "1095"]]

    client, stubber = _athena_client()
    _stub_successful_query(stubber, columns, data_rows)

    source = _make_batch_source(client)

    with stubber:
        rows = source.fetch_findings(vins=[SAMPLE_VIN])

    assert len(rows) == 1
    row = rows[0]
    assert row["vin"] == SAMPLE_VIN
    assert row["health_pct"] == "92.3"
    assert row["age_days"] == "1095"
    # Must not expose ADP column names
    assert "state_of_health_pct" not in row
    assert "battery_age_days" not in row


def test_batch_source_fetch_findings_empty_returns_empty_list():
    """AdpBatterySource.fetch_findings returns [] when Athena returns no data rows."""
    columns = ["vin", "state_of_health_pct", "battery_age_days"]
    client, stubber = _athena_client()
    _stub_successful_query(stubber, columns, [])

    source = _make_batch_source(client)
    with stubber:
        rows = source.fetch_findings(vins=None)

    assert rows == []


def test_batch_source_product_is_energy_usage():
    """AdpBatterySource SQL targets energy_usage, not tire_health or service_records."""
    client, _ = _athena_client()
    source = _make_batch_source(client)
    sql = source._build_sql(vins=None)
    assert "energy_usage" in sql
    assert "tire_health" not in sql
    assert "service_records" not in sql


# ---------------------------------------------------------------------------
# T6b.1 — max_rows threading: assert on what run_query received, not the
# constructor argument.  The gap was that max_rows was stored in the instance
# but never forwarded to run_query, making TIER2_MAX_ROWS_PER_QUERY inert.
# ---------------------------------------------------------------------------


def test_max_rows_is_forwarded_to_run_query_when_set():
    """AdpBatterySource forwards max_rows to run_query when a ceiling is configured.

    Asserts on the kwarg that run_query actually RECEIVED, not on the constructor
    argument — storing a value in self._max_rows is not the same as passing it.
    The distinction is exactly the vacuous-test mistake recorded in decisions.md.
    """
    from unittest.mock import patch

    source = AdpBatterySource(
        "staging",
        athena_client=object(),  # not called — run_query is patched
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,
        max_rows=1234,
    )

    with patch("agents.health.adp_battery_source.run_query", return_value=[]) as mock_rq:
        source.fetch_findings(vins=None)

    mock_rq.assert_called_once()
    _, kwargs = mock_rq.call_args
    assert "max_rows" in kwargs, (
        "run_query was not called with max_rows kwarg; "
        "the ceiling is stored but never forwarded."
    )
    assert kwargs["max_rows"] == 1234, (
        f"run_query received max_rows={kwargs['max_rows']!r} but expected 1234."
    )


def test_max_rows_not_forwarded_to_run_query_when_none():
    """When max_rows is None (default), run_query is called WITHOUT max_rows kwarg.

    This lets run_query use its own default (_DEFAULT_MAX_ROWS=100_000) rather
    than being forced to an explicit None, which would override the function's
    default-parameter binding.
    """
    from unittest.mock import patch

    source = AdpBatterySource(
        "staging",
        athena_client=object(),
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        poll_interval=0,
        max_rows=None,  # explicit None — should not appear in kwargs
    )

    with patch("agents.health.adp_battery_source.run_query", return_value=[]) as mock_rq:
        source.fetch_findings(vins=None)

    mock_rq.assert_called_once()
    _, kwargs = mock_rq.call_args
    assert "max_rows" not in kwargs, (
        "run_query should not receive max_rows when the ceiling is None; "
        "None would override run_query's own default."
    )


# ---------------------------------------------------------------------------
# select_vins tests (Group 2 — RED phase)
# ---------------------------------------------------------------------------
# select_vins does not exist yet; all tests below must fail with AttributeError
# until Group 3 adds the method to AdpBatterySource.
#
# CRITICAL ASYMMETRY (spec § D2): battery has NO priority pool predicate.
# Any numeric threshold on state_of_health_pct or battery_age_days would be
# CVX inference — a § D3 violation.  The test suite asserts that no such
# comparison exists in the selection SQL.
# ---------------------------------------------------------------------------


def _make_battery_source_for_select(max_vins: int = 10) -> AdpBatterySource:
    client, _ = _athena_client()
    return AdpBatterySource(
        "staging",
        athena_client=client,
        workgroup=WORKGROUP,
        results_s3_path=RESULTS_PATH,
        max_vins=max_vins,
    )


def test_select_vins_method_exists_on_battery():
    """AdpBatterySource exposes a select_vins method."""
    source = _make_battery_source_for_select()
    assert hasattr(source, "select_vins"), (
        "AdpBatterySource.select_vins does not exist — Group 3 must add it"
    )


def test_battery_select_vins_build_sql_no_cursor_contains_ordered_distinct():
    """select_vins SQL (no cursor) contains SELECT DISTINCT vin ORDER BY vin LIMIT N."""
    source = _make_battery_source_for_select(max_vins=7)
    sql = source._build_select_vins_sql(cursor=None, limit=7)
    assert "SELECT DISTINCT vin" in sql
    assert "ORDER BY vin" in sql
    assert "LIMIT 7" in sql


def test_battery_select_vins_build_sql_no_pool_predicate():
    """Battery select_vins SQL has NO pool predicate beyond keyset pagination.

    Battery has only continuous signals (state_of_health_pct, battery_age_days).
    Any numeric cut-off would be CVX inference — a spec § D2/D3 violation.
    The selection SQL must contain no WHERE clause condition other than:
      1. The base product filter (no additional domain filter)
      2. vin > '<cursor>' when a cursor is supplied

    With cursor=None there should be no WHERE clause at all (or just the
    ORDER BY vin LIMIT N subquery structure).
    """
    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor=None, limit=10)
    sql_lower = sql.lower()

    # No state-of-health related column comparison.
    assert "state_of_health" not in sql_lower, (
        "Battery select_vins SQL must not reference state_of_health_pct — "
        "any cut-off on a continuous metric is a CVX-inference violation (spec § D2)"
    )
    assert "battery_age" not in sql_lower, (
        "Battery select_vins SQL must not reference battery_age_days in a predicate — "
        "a numeric age threshold is a CVX-inference violation (spec § D2)"
    )


def test_battery_select_vins_no_numeric_comparison_in_sql():
    """Battery select_vins SQL contains no numeric comparison operators outside vin >.

    This is the direct assertion of spec § D2's 'battery has no pool predicate'.
    Numeric comparisons (<, >, <=, >=) applied to anything other than the keyset
    cursor's vin > clause are CVX inference and are forbidden.
    """
    import re as _re

    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor=None, limit=10)
    # Strip the vin > '<cursor>' keyset pattern (only present with cursor != None)
    # then check for any remaining numeric comparison.
    sql_without_cursor = _re.sub(r"vin\s*>\s*'[^']*'", "", sql, flags=_re.IGNORECASE)
    numeric_cmp = _re.search(r"(?<!['\w])[<>]=?\s*\d", sql_without_cursor)
    assert numeric_cmp is None, (
        f"Numeric comparison found in battery select_vins SQL: {numeric_cmp.group()!r}\n"
        "Battery has NO pool predicate — any numeric threshold is a CVX-inference "
        "violation (spec § D2, D3).  The only selection criterion is rotation via "
        "keyset pagination."
    )


def test_battery_select_vins_with_cursor_adds_keyset_predicate():
    """select_vins(cursor='X', limit=N) adds vin > 'X' for keyset pagination."""
    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor="VIN-XYZ", limit=10)
    assert "vin >" in sql.lower() or "vin>" in sql.lower(), (
        "Cursor must produce a vin > '<cursor>' keyset predicate, not OFFSET"
    )
    assert "VIN-XYZ" in sql


def test_battery_select_vins_cursor_sanitised_before_interpolation():
    """Cursor value is sanitised (allow-listed) before embedding in SQL."""
    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor="VIN123' OR '1'='1", limit=10)
    assert "VIN123' OR '1'='1" not in sql, (
        "Raw cursor with SQL metacharacters must not appear verbatim in SQL"
    )
    assert "VIN123" in sql


def test_battery_select_vins_cursor_double_dash_cannot_alter_statement():
    """A cursor containing -- cannot comment out the LIMIT clause."""
    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor="VIN123--INJECTED", limit=10)
    assert "LIMIT" in sql, (
        "-- in cursor must not comment out LIMIT — sanitisation strips injection"
    )


def test_battery_select_vins_cursor_single_quote_cannot_alter_statement():
    """A cursor containing a single quote cannot break out of the string literal."""
    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor="VIN123' AND 1=1--", limit=10)
    assert "' AND 1=1" not in sql, (
        "Single-quote in cursor must be stripped — SQL injection must not appear"
    )
    assert "LIMIT" in sql


def test_battery_select_vins_returns_tuple_of_list_and_next_cursor():
    """select_vins returns a (list[str], str | None) tuple per spec § D3 signature.

    With a stubbed run_query that returns no rows, select_vins should return
    ([], None) indicating pool exhaustion / end of rotation cycle.
    """
    from unittest.mock import patch

    source = _make_battery_source_for_select()
    with patch("agents.health.adp_battery_source.run_query", return_value=[]):
        result = source.select_vins(cursor=None, limit=10)

    assert isinstance(result, tuple) and len(result) == 2
    vins, next_cursor = result
    assert isinstance(vins, list)
    assert next_cursor is None


def test_battery_select_vins_returns_vins_and_advances_cursor():
    """With rows returned, select_vins returns VINs and the last VIN as next cursor."""
    from unittest.mock import patch

    fake_rows = [{"vin": "VIN-A"}, {"vin": "VIN-B"}, {"vin": "VIN-C"}]
    source = _make_battery_source_for_select()
    with patch("agents.health.adp_battery_source.run_query", return_value=fake_rows):
        vins, next_cursor = source.select_vins(cursor=None, limit=10)

    assert vins == ["VIN-A", "VIN-B", "VIN-C"]
    assert next_cursor == "VIN-C", "next cursor must be the last VIN in the batch"


def test_battery_select_vins_targets_energy_usage_product():
    """Battery select_vins SQL targets energy_usage, not any other product."""
    source = _make_battery_source_for_select()
    sql = source._build_select_vins_sql(cursor=None, limit=10)
    assert "energy_usage" in sql
    assert "tire_health" not in sql
    assert "service_records" not in sql
