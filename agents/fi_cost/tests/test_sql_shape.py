"""
SQL shape tests for fi_cost Tier 2 component (task 3.1).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D6 step 1
Decisions: decisions.md 2026-10-05 item 6 (energy outlier rule; no cap on selection query)

These tests verify structural properties of the SQL templates.  They catch SQL
mutations that Python unit tests cannot reach (because stubs return rows regardless
of the SQL text).

Mutation checks:
  M-OR-energy: comment out is_energy_outlier from WHERE → test_sql_has_is_energy_outlier fails.
  M-OR-total:  comment out is_outlier from WHERE → test_sql_has_is_outlier fails.
  M-GREATEST:  replace GREATEST(…) with a single-column ORDER BY →
               test_selection_sql_orders_by_greatest fails.
  M-LIMIT:     add LIMIT clause to cap selection → test_selection_not_capped fails.

These run green (no xfail) because sql/ templates exist.
"""
from __future__ import annotations

import re
from pathlib import Path

# SQL template paths — relative to the repo root
_SQL_DIR = Path(__file__).parent.parent / "sql"
_SELECT_SQL = (_SQL_DIR / "select_anomalies.sql").read_text().lower()
_EVIDENCE_SQL = (_SQL_DIR / "gather_evidence.sql").read_text().lower()


# ---------------------------------------------------------------------------
# Selection SQL must contain both outlier flags (OR logic)
# ---------------------------------------------------------------------------


def test_selection_sql_has_is_outlier_flag():
    """select_anomalies.sql must reference is_outlier in the WHERE clause.

    Mutation M-OR-total: dropping is_outlier makes total-only outliers invisible.
    """
    uncommented = re.sub(r'--[^\n]*', '', _SELECT_SQL)
    # Extract WHERE clause (between WHERE and ORDER BY)
    where_match = re.search(r'\bwhere\b(.*?)\border\s+by\b', uncommented, re.DOTALL | re.IGNORECASE)
    where_clause = where_match.group(1) if where_match else uncommented
    assert "is_outlier" in where_clause, (
        "select_anomalies.sql must reference is_outlier in the WHERE clause; "
        "dropping it makes total-only outliers invisible (M-OR-total)"
    )


def test_selection_sql_has_is_energy_outlier_flag():
    """select_anomalies.sql must reference is_energy_outlier in the WHERE clause.

    Mutation M-OR-energy: dropping is_energy_outlier makes energy-only outliers invisible.
    decisions.md 2026-10-05 item 6: selection is is_outlier OR is_energy_outlier.
    """
    uncommented = re.sub(r'--[^\n]*', '', _SELECT_SQL)
    where_match = re.search(r'\bwhere\b(.*?)\border\s+by\b', uncommented, re.DOTALL | re.IGNORECASE)
    where_clause = where_match.group(1) if where_match else uncommented
    assert "is_energy_outlier" in where_clause, (
        "select_anomalies.sql must reference is_energy_outlier in the WHERE clause; "
        "dropping it makes energy-only outliers invisible (M-OR-energy)"
    )


def test_selection_sql_uses_or_between_flags():
    """The WHERE clause must combine flags with OR, not AND.

    OR: a vehicle with either flag is selected.
    AND: a vehicle needs both flags → energy-only and total-only outliers are lost.
    """
    uncommented = re.sub(r'--[^\n]*', '', _SELECT_SQL)
    # Check that both flags appear with an OR in the where clause context.
    # A simple check: find (is_outlier ... or ... is_energy_outlier) pattern
    assert re.search(
        r'is_outlier\b.*\bor\b.*\bis_energy_outlier|'
        r'is_energy_outlier\b.*\bor\b.*\bis_outlier',
        uncommented,
        re.DOTALL,
    ), (
        "select_anomalies.sql must combine is_outlier and is_energy_outlier with OR. "
        "Using AND would drop vehicles with only one flag set."
    )


# ---------------------------------------------------------------------------
# Ordering must use GREATEST (both excess types)
# ---------------------------------------------------------------------------


def test_selection_sql_orders_by_greatest():
    """select_anomalies.sql must use GREATEST(…) in ORDER BY.

    Mutation M-GREATEST: replacing with a single column drops the energy-excess
    dimension from ordering, so vehicles with large energy excess but small total
    excess are deprioritized and may be reached too late.
    decisions.md 2026-10-05 item 6: ordered by the larger of total excess
    and energy_excess_usd.
    """
    uncommented = re.sub(r'--[^\n]*', '', _SELECT_SQL)
    assert "greatest(" in uncommented, (
        "select_anomalies.sql must use GREATEST(…) in ORDER BY to order by the "
        "larger of total excess and energy_excess_usd (decisions.md 2026-10-05 item 6)"
    )


def test_selection_sql_references_energy_excess_usd_in_order_by():
    """GREATEST clause must include energy_excess_usd."""
    uncommented = re.sub(r'--[^\n]*', '', _SELECT_SQL)
    assert "energy_excess_usd" in uncommented, (
        "select_anomalies.sql must reference energy_excess_usd in ORDER BY GREATEST"
    )


# ---------------------------------------------------------------------------
# Selection query must NOT be capped (decisions 2026-10-05 item 6)
# ---------------------------------------------------------------------------


def test_selection_not_capped_by_limit():
    """select_anomalies.sql must NOT contain a LIMIT clause.

    decisions.md 2026-10-05, item 6: max_diagnoses_per_run caps model calls
    after D6 step 3's unchanged-skip, NOT the selection query.  A LIMIT on the
    SQL would mean the same top-N rows are selected (and then skipped) on every
    daily run, and later vehicles are never reached.
    """
    uncommented = re.sub(r'--[^\n]*', '', _SELECT_SQL)
    assert "limit " not in uncommented, (
        "select_anomalies.sql must NOT contain LIMIT. "
        "max_diagnoses_per_run caps model calls at D6 step 3, not the selection query. "
        "A LIMIT here would repeatedly select the same top-N and skip them, blocking "
        "the rest from ever being reached. (decisions.md 2026-10-05, item 6)"
    )


# ---------------------------------------------------------------------------
# Evidence SQL must contain required column references
# ---------------------------------------------------------------------------


def test_evidence_sql_has_fleet_id():
    """gather_evidence.sql must include fleet_id (needed for queue projection)."""
    assert "fleet_id" in _EVIDENCE_SQL, "gather_evidence.sql must include fleet_id"


def test_evidence_sql_has_energy_excess_usd():
    """gather_evidence.sql must include cost.energy_excess_usd (decisions 2026-10-05)."""
    assert "energy_excess_usd" in _EVIDENCE_SQL, (
        "gather_evidence.sql must include energy_excess_usd "
        "(D8 evidence key added in decisions.md 2026-10-05)"
    )


def test_evidence_sql_has_delta_columns():
    """gather_evidence.sql must include per-category delta columns (D8 evidence)."""
    for col in ["delta_energy_cohort", "delta_maintenance_cohort"]:
        assert col in _EVIDENCE_SQL, (
            f"gather_evidence.sql must reference {col} for D8 evidence entries"
        )


def test_evidence_sql_has_energy_method():
    """gather_evidence.sql must include energy_method (decisions 2026-10-05)."""
    assert "energy_method" in _EVIDENCE_SQL, (
        "gather_evidence.sql must include energy_method "
        "(D8 evidence key: cost.energy_method)"
    )


def test_evidence_sql_is_single_query():
    """gather_evidence.sql must be a single query (D6 step 2, decisions item 5).

    The query must end in a single SELECT statement, using CTEs rather than
    multiple semicolon-separated statements.
    """
    uncommented = re.sub(r'--[^\n]*', '', _EVIDENCE_SQL)
    # Strip leading/trailing whitespace; count semicolons
    statement_terminators = uncommented.count(";")
    # CTEs use commas, not semicolons; only the terminating semicolon is allowed
    assert statement_terminators <= 1, (
        "gather_evidence.sql must be a single query (one semicolon at most). "
        "Multiple statements would be separate Athena calls, not one query for all VINs."
    )



# ---------------------------------------------------------------------------
# Fix 5 — Structural: every FROM/JOIN database must be an allowed Tier 2 source
# (task 3.1 item 5 — pre-review fix)
# ---------------------------------------------------------------------------


def _extract_database_refs(sql_text: str) -> list[str]:
    """Extract all 'database.table' references from FROM and JOIN clauses.

    Strips SQL comments, then finds tokens of the form
    ``adp_${stage}_<something>.<table>`` in FROM/JOIN positions.
    Returns the set of database-name templates (with the stage placeholder).
    """
    # Strip single-line comments
    stripped = re.sub(r'--[^\n]*', '', sql_text)
    # Match adp_${stage}_<word>.<identifier> anywhere (FROM/JOIN already present)
    pattern = re.compile(r'\badp_\$\{stage\}_(\w+)\.\w+', re.IGNORECASE)
    return [f"adp_${{stage}}_{m.group(1)}" for m in pattern.finditer(stripped)]


_ALLOWED_DATABASES = frozenset({
    "adp_${stage}_fleet_cost",
    "adp_${stage}_service_records",
})

_BOTH_SQL_TEXTS = {
    "select_anomalies.sql": (_SQL_DIR / "select_anomalies.sql").read_text(),
    "gather_evidence.sql": (_SQL_DIR / "gather_evidence.sql").read_text(),
}

#: Every SQL file the component runs, with the databases it may name (task 4.1 added
#: the last two).  The Tier 2 role holds Lake Formation grants on exactly the union
#: of these plus the views' base tables (vsa-analytics-stack.ts; pinned in
#: agents/tier2/tests/test_fi_cost_grants_pinned.py).
_ALLOWED_BY_FILE = {
    "select_anomalies.sql": _ALLOWED_DATABASES,
    "gather_evidence.sql": _ALLOWED_DATABASES,
    "latest_month.sql": frozenset({"adp_${stage}_fleet_cost"}),
    "sell_timing_inputs.sql": frozenset({"adp_${stage}_fleet_cost", "adp_${stage}_vehicle_pricing"}),
}


def test_every_sql_file_has_an_allowlist_and_names_only_its_databases():
    """A new SQL file can't skip the check: the directory listing must equal the table."""
    on_disk = {p.name for p in _SQL_DIR.glob("*.sql")}
    assert on_disk == set(_ALLOWED_BY_FILE), (on_disk, set(_ALLOWED_BY_FILE))
    for filename, allowed in _ALLOWED_BY_FILE.items():
        refs = _extract_database_refs((_SQL_DIR / filename).read_text())
        assert refs, f"{filename} names no ADP database (anti-vacuity)"
        assert set(refs) <= allowed, (filename, sorted(set(refs) - allowed))


def test_sql_databases_are_only_allowed_sources():
    """Every FROM/JOIN database in both SQL files must be an allowed Tier 2 source.

    Allowed databases (task 4.2 Tier 2 role grants):
      adp_${stage}_fleet_cost
      adp_${stage}_service_records

    Any reference to adp_${stage}_cms_ingest (or another database) must fail
    this test.

    Mutation M-DB: restore 'adp_${stage}_cms_ingest.service_records' in
    gather_evidence.sql → this test fails immediately, naming the bad database.
    """
    for filename, sql_text in _BOTH_SQL_TEXTS.items():
        refs = _extract_database_refs(sql_text)
        for ref in refs:
            assert ref in _ALLOWED_DATABASES, (
                f"{filename}: database {ref!r} is not in the allowed Tier 2 sources "
                f"{sorted(_ALLOWED_DATABASES)}.  "
                "Only adp_${{stage}}_fleet_cost and adp_${{stage}}_service_records "
                "are granted to the Tier 2 role (task 4.2)."
            )


def test_gather_evidence_does_not_reference_cms_ingest():
    """gather_evidence.sql must not reference adp_${stage}_cms_ingest.

    This is a targeted regression guard for the architect's fix
    (adp_${stage}_cms_ingest → adp_${stage}_service_records).

    Mutation M-DB: revert the rename → 'cms_ingest' appears in the text and
    this assertion fails.
    """
    evidence_sql = (_SQL_DIR / "gather_evidence.sql").read_text()
    assert "cms_ingest" not in evidence_sql, (
        "gather_evidence.sql must not reference adp_${stage}_cms_ingest. "
        "The correct database is adp_${stage}_service_records."
    )



# ---------------------------------------------------------------------------
# Fix 2 — svc_window upper bound (M-5 to M inclusive)
# ---------------------------------------------------------------------------


def test_svc_window_has_upper_bound():
    """gather_evidence.sql svc_window must have an upper bound on service_month.

    The window is M-5 to M inclusive (six months).  Without the upper bound,
    a record for month M+1 written after the evidence query would change the
    max_repeat_count and therefore the inputs_hash for month M (D11).

    Mutation M-WINDOW-UPPER: remove the upper bound clause → no reference to
    '<= date_parse' in the svc_window block → this test fails.
    """
    evidence_sql = (_SQL_DIR / "gather_evidence.sql").read_text()
    # Search for the CTE definition, not a comment reference
    cte_marker = "svc_window AS ("
    svc_start = evidence_sql.find(cte_marker)
    assert svc_start >= 0, "svc_window CTE must exist in gather_evidence.sql"
    # Extract up to 800 chars from the CTE definition (covers the full WHERE)
    svc_block = evidence_sql[svc_start:svc_start + 800].lower()
    # Strip comments
    svc_block = re.sub(r'--[^\n]*', '', svc_block)
    assert "<=" in svc_block, (
        "svc_window must have an upper bound (service_month <= ...) so records "
        "for later months cannot change month M's hash (D11)."
    )
    assert "date_parse" in svc_block, (
        "svc_window must use DATE_PARSE to compute the upper bound."
    )


def test_svc_window_starts_at_minus_5_months():
    """svc_window lower bound must be -5 months (M-5 to M = six months).

    The old code used -6 which meant M-6 or later (no upper bound); the correct
    window is M-5 through M (six months inclusive).

    Mutation M-WINDOW-LOWER: change -5 to -6 → 'month', -6 still matches this
    test if the test only checks for presence of '-5'; test checks exact value.
    """
    evidence_sql_raw = (_SQL_DIR / "gather_evidence.sql").read_text()
    cte_marker = "svc_window AS ("
    svc_start = evidence_sql_raw.find(cte_marker)
    assert svc_start >= 0, "svc_window CTE must exist in gather_evidence.sql"
    svc_block = evidence_sql_raw[svc_start:svc_start + 800]
    # Strip comments
    svc_block_nocomment = re.sub(r'--[^\n]*', '', svc_block)
    assert "-5," in svc_block_nocomment or "-5\n" in svc_block_nocomment or "-5)" in svc_block_nocomment, (
        "svc_window lower bound must be -5 months (DATE_ADD('month', -5, ...)) "
        "so the window is M-5 through M (six calendar months)."
    )
    assert "-6," not in svc_block_nocomment and "-6\n" not in svc_block_nocomment, (
        "svc_window must NOT use -6; the correct lower bound is -5."
    )


# ---------------------------------------------------------------------------
# Fix 4 — COALESCE total-excess term in select_anomalies.sql
# ---------------------------------------------------------------------------


def test_select_anomalies_coalesces_total_excess():
    """GREATEST() in select_anomalies.sql must COALESCE the total-excess term.

    Trino/Athena GREATEST returns NULL when any argument is NULL; DuckDB ignores
    NULLs.  A NULL cohort_median makes (cost_per_mile - cohort_median) * miles
    NULL, which makes the entire GREATEST NULL on Trino and breaks the ordering.

    The fix is COALESCE((cost_per_mile - cohort_median) * miles, 0.0).

    Mutation M-COALESCE: replace with (cost_per_mile - cohort_median) * COALESCE(miles, 0.0)
    → the outer COALESCE is gone, so NULL cohort_median still makes GREATEST NULL on Trino
    → this structural test fails.
    """
    select_sql = (_SQL_DIR / "select_anomalies.sql").read_text().lower()
    uncommented = re.sub(r'--[^\n]*', '', select_sql)
    # The COALESCE must wrap the entire (cost_per_mile - cohort_median) expression,
    # not just COALESCE(miles, ...) inside it.  Check for the specific pattern:
    # coalesce( (cost_per_mile - cohort_median) * ...
    assert re.search(
        r'coalesce\s*\(\s*\(\s*cost_per_mile\s*-\s*cohort_median\s*\)',
        uncommented,
    ), (
        "select_anomalies.sql ORDER BY must use "
        "COALESCE((cost_per_mile - cohort_median) * miles, 0.0) "
        "— the COALESCE must wrap the whole subtraction expression so that "
        "NULL cohort_median produces 0.0 on Trino (GREATEST returns NULL otherwise). "
        "Mutation M-COALESCE: using COALESCE only around 'miles' leaves the NULL "
        "cohort_median path unprotected."
    )


# ---------------------------------------------------------------------------
# Fix 1a — No APPROX_PERCENTILE in gather_evidence.sql
# ---------------------------------------------------------------------------


def test_gather_evidence_has_no_approx_percentile():
    """gather_evidence.sql must not use APPROX_PERCENTILE.

    Spec D11 (deterministic evidence): approximate functions make evidence
    non-deterministic across query re-runs.  The exact median pattern from
    ADP's vehicle_cost_baseline.sql (cohort_all_ranked / cohort_stats) must
    be used instead.

    Mutation M-APPROX: restore APPROX_PERCENTILE → this test fails immediately.
    """
    evidence_sql = (_SQL_DIR / "gather_evidence.sql").read_text()
    # Strip SQL comments before checking — a comment explaining the replacement
    # may legitimately mention the old function name.
    uncommented = re.sub(r'--[^\n]*', '', evidence_sql).lower()
    assert "approx_percentile" not in uncommented, (
        "gather_evidence.sql must not use APPROX_PERCENTILE (outside comments). "
        "Use the exact median pattern (ROW_NUMBER/COUNT) for fleet_median_miles "
        "and cohort_kwh_per_100mi (spec D11 determinism)."
    )


def test_gather_evidence_uses_row_number_for_median():
    """gather_evidence.sql must use ROW_NUMBER for the exact median computation."""
    evidence_sql = (_SQL_DIR / "gather_evidence.sql").read_text().lower()
    assert "row_number()" in evidence_sql, (
        "gather_evidence.sql must use ROW_NUMBER() for the exact median pattern."
    )
