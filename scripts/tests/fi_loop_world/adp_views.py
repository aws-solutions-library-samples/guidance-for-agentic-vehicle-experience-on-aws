"""ADP view SQL piece — registers the real ADP 9.1 view SQL in FakeAthena's DuckDB.

Two views are registered from ADP commit 9bb82e96, loaded via ``git show`` and
translated for DuckDB through the world's Athena translator (plus extensions for
CARDINALITY).  Both names match the live Glue views so all existing queries
against these names are directed to the real SQL logic.

W11 fix: previously this module registered a hand-written "focused version" of
``recall_vehicle_status`` (its own docstring) rather than the real 9.1 SQL.
``register_adp_views`` now reads the SQL text via ``git show 9bb82e96:…`` and
fails loudly if that extraction fails — the world should never fall back to
a different commit.

Tables created (in addition to vehicle_build_components from this module):

  adp_{stage}_fleet_recall.parameters
      One row with ``signature_lookback_days`` so the population CTE can
      COALESCE per-campaign and global lookback days.

  adp_{stage}_service_records.service_records (altered)
      Adds ``service_id VARCHAR`` if absent — needed by ``remedied_adp`` CTE.

DuckDB VIEW logic:

1. recall_campaigns_received
   UNION ALL of ``recall_campaigns`` (Stage C base table) and
   ``recall_campaigns_issued`` rows with status = 'issued' and id not in Stage C.

2. recall_vehicle_status
   Full 285-line view: population (managed VINs × received campaigns), lot rule
   (issued campaigns with component_lots restrict to build-component matches),
   confirmed_signals (DTC match in service_records within lookback window),
   remedied_dms (DMS RO reaching Ready/Delivered/Invoiced with recall line),
   remedied_adp (service_records with service_type='safety_recall').

Source citations (file:line from ADP 9bb82e96):
  recall_campaigns_received.sql : 29–47 (UNION ALL, issued branch)
  recall_vehicle_status.sql     : 1–285 (full view)
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

_ADP_ROOT = Path(os.environ.get(
    "FI_LOOP_WORLD_ADP_REPO",
    str(Path.home() / "automotive-data-platform-on-aws")
))

#: Commit this world piece was authored against (ADP wip/fi-health-d-g9).
ADP_COMMIT = "9bb82e96"

_RECEIVED_SQL_RELPATH = (
    "platform-foundation/source/data-products/recall_campaigns/views/"
    "recall_campaigns_received.sql"
)
_STATUS_SQL_RELPATH = (
    "platform-foundation/source/data-products/recall_campaigns/views/"
    "recall_vehicle_status.sql"
)

# ── column definitions ────────────────────────────────────────────────────────

#: vehicle_build_components Glue columns (ADP 9bb82e96 FleetHealthStack).
VEHICLE_BUILD_COMPONENTS_COLUMNS: list[tuple[str, str]] = [
    ("vin", "string"),
    ("component_id", "string"),
    ("supplier_id", "string"),
    ("supplier_lot", "string"),
    ("component_category", "string"),
    ("installed_date", "date"),
    ("part_number", "string"),
]


# ── SQL loading + translation ─────────────────────────────────────────────────

def _read_sql_from_git(relpath: str) -> str:
    """Read the file at *relpath* from ADP commit 9bb82e96 via ``git show``.

    Raises ``FileNotFoundError`` when the ADP repo is absent or the commit is
    not reachable.  Fails loudly — no fallback to the working tree.
    """
    if not _ADP_ROOT.exists():
        raise FileNotFoundError(
            f"ADP repo not found at {_ADP_ROOT}. "
            f"Set FI_LOOP_WORLD_ADP_REPO to the ADP repo root "
            f"(must contain commit {ADP_COMMIT})."
        )
    git_ref = f"{ADP_COMMIT}:{relpath}"
    try:
        proc = subprocess.run(
            ["git", "-C", str(_ADP_ROOT), "show", git_ref],
            capture_output=True, check=True,
        )
        return proc.stdout.decode("utf-8")
    except subprocess.CalledProcessError as exc:
        raise FileNotFoundError(
            f"git show {git_ref!r} failed in {_ADP_ROOT}: "
            f"{exc.stderr.decode()!r}. "
            f"Ensure commit {ADP_COMMIT} is present in the repo."
        ) from exc


# Patterns that indicate SQL that cannot be run directly in DuckDB without
# translation.  After applying the translator, none should remain; their
# presence causes a loud failure.
_UNTRANSLATABLE_PATTERNS: list[str] = [
    r"\bfrom_iso8601_timestamp\s*\(",
    r"\bjson_extract_scalar\s*\(",
    r"\bjson_parse\s*\(",
    r"\bMIN_BY\s*\(",
    r"\bCARDINALITY\s*\(",
    r"\barray_join\s*\(",
    r"\${",                        # leftover Athena ${stage} substitution
]


def _translate_for_duckdb(sql: str, *, stage: str) -> str:
    """Translate Athena/Presto SQL to DuckDB equivalents.

    Applies:
    1. ``${stage}`` → *stage* (Athena DDL interpolation).
    2. All translations from the world's ``_translate_athena_to_duckdb``.
    3. ``CARDINALITY(`` → ``array_length(`` (DuckDB only supports
       CARDINALITY on MAPs, not arrays).

    After translation, scans for known untranslatable patterns and raises
    ``ValueError`` if any remain.
    """
    # 1. Athena ${stage} substitution (must happen first).
    sql = sql.replace("${stage}", stage)

    # 2. Apply existing world translator (CONTAINS, MIN_BY, from_iso8601_timestamp,
    #    json_extract_scalar, json_parse, ARRAY(JSON), date_add).
    sql = sql.replace("array_join(", "array_to_string(")
    sql = re.sub(r"\bCONTAINS\s*\(", "list_contains(", sql)
    sql = re.sub(r"\bMIN_BY\s*\(", "arg_min(", sql)
    sql = re.sub(
        r"date_add\('day',\s*(-?[\w.]+),\s*([^)]+?)\)",
        lambda m: f"({m.group(2).strip()} + ({m.group(1).strip()}) * INTERVAL '1 DAY')",
        sql,
    )
    sql = re.sub(
        r"\bfrom_iso8601_timestamp\s*\(([^)]+)\)",
        r"CAST(\1 AS TIMESTAMPTZ)",
        sql,
    )
    sql = re.sub(r"\bjson_extract_scalar\s*\(", "json_extract_string(", sql)
    sql = re.sub(r"\bjson_parse\s*\(([^)]+)\)", r"(\1)::JSON", sql)
    sql = sql.replace("ARRAY(JSON)", "JSON[]")

    # 3. CARDINALITY on arrays → array_length (DuckDB CARDINALITY only works on MAPs).
    sql = re.sub(r"\bCARDINALITY\s*\(", "array_length(", sql)

    # 4. Fail loudly if untranslatable patterns remain.
    for pattern in _UNTRANSLATABLE_PATTERNS:
        m = re.search(pattern, sql, re.IGNORECASE)
        if m:
            raise ValueError(
                f"adp_views: SQL still contains untranslatable construct "
                f"{pattern!r} (matched {m.group()!r}) after translation. "
                f"Extend _translate_for_duckdb to handle it."
            )

    return sql


def _extract_view_body(sql: str, view_name_fragment: str) -> str:
    """Extract the SQL after ``AS`` from a ``CREATE OR REPLACE VIEW … AS`` statement.

    The full ``CREATE OR REPLACE VIEW`` header is replaced with the DuckDB
    equivalent ``CREATE OR REPLACE VIEW <view_name_fragment> AS``.
    """
    # The SQL contains only one CREATE statement.
    # Split on the first 'AS' that follows 'CREATE OR REPLACE VIEW …'.
    m = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+\S+\s+AS\s",
        sql, re.IGNORECASE
    )
    if not m:
        raise ValueError(
            f"adp_views: could not find 'CREATE OR REPLACE VIEW … AS' "
            f"in SQL for {view_name_fragment!r}."
        )
    body = sql[m.end():]
    return f"CREATE OR REPLACE VIEW {view_name_fragment} AS\n{body}"


# ── registration ─────────────────────────────────────────────────────────────

def register_adp_views(athena: Any, *, stage: str = "staging") -> None:
    """Register the real ADP 9.1 view SQL in *athena*'s DuckDB connection.

    Steps:
    1. Load ``recall_campaigns_received.sql`` and ``recall_vehicle_status.sql``
       from ADP commit 9bb82e96 via ``git show``.
    2. Translate from Athena to DuckDB via ``_translate_for_duckdb``.
    3. Create missing tables: ``parameters``, ``vehicle_build_components``.
    4. Add ``service_id`` column to ``service_records`` if absent (needed by
       the ``remedied_adp`` CTE).
    5. Register both VIEWs via ``CREATE OR REPLACE VIEW``, dropping any
       pre-existing data tables of the same name first (the base world creates
       them as data tables via ``_refresh_lake``).

    Idempotent: ``CREATE OR REPLACE VIEW`` tolerates re-registration.
    """
    con = athena.con

    # ── 1. Load real SQL from git ──────────────────────────────────────────
    received_sql_raw = _read_sql_from_git(_RECEIVED_SQL_RELPATH)
    status_sql_raw = _read_sql_from_git(_STATUS_SQL_RELPATH)

    # ── 2. Translate ──────────────────────────────────────────────────────
    received_sql = _translate_for_duckdb(received_sql_raw, stage=stage)
    status_sql = _translate_for_duckdb(status_sql_raw, stage=stage)

    # ── 3. Ensure supporting schemas and tables exist ─────────────────────
    con.execute(f"CREATE SCHEMA IF NOT EXISTS adp_{stage}_engineering")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS adp_{stage}_fleet_recall")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS adp_{stage}_vehicle_identity")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS adp_{stage}_service_records")

    # vehicle_build_components (lot rule JOIN target)
    con.execute(
        f"CREATE TABLE IF NOT EXISTS adp_{stage}_engineering.vehicle_build_components ("
        f'"vin" VARCHAR, "component_id" VARCHAR, "supplier_id" VARCHAR, '
        f'"supplier_lot" VARCHAR, "component_category" VARCHAR, '
        f'"installed_date" DATE, "part_number" VARCHAR'
        f")"
    )

    # parameters (one-row table with signature_lookback_days default)
    con.execute(
        f"CREATE TABLE IF NOT EXISTS adp_{stage}_fleet_recall.parameters ("
        f'"signature_lookback_days" INTEGER'
        f")"
    )
    # Seed one row with a 365-day default if the table is empty.
    params_count = con.execute(
        f"SELECT COUNT(*) FROM adp_{stage}_fleet_recall.parameters"
    ).fetchone()[0]
    if params_count == 0:
        con.execute(
            f"INSERT INTO adp_{stage}_fleet_recall.parameters "
            f"(signature_lookback_days) VALUES (365)"
        )

    # dms_{stage}.repair_orders — needed by the remedied_dms CTE.
    # In FiLoopWorld, this is created by fi_rw_world._refresh_lake.
    # For standalone tests, create an empty table here.
    con.execute(f"CREATE SCHEMA IF NOT EXISTS dms_{stage}")
    con.execute(
        f"CREATE TABLE IF NOT EXISTS dms_{stage}.repair_orders ("
        f'"_change_kind" VARCHAR, "_seq_no" VARCHAR, '
        f'"ro_id" VARCHAR, "vehicle_vin" VARCHAR, "status" VARCHAR, '
        f'"updated_at" VARCHAR, "lines" VARCHAR, "record_kind" VARCHAR'
        f")"
    )
    con.execute(
        f"CREATE TABLE IF NOT EXISTS adp_{stage}_fleet_recall.managed_vehicles ("
        f'"vin" VARCHAR, "fleet_id" VARCHAR, "snapshot_date" DATE'
        f")"
    )

    # vehicle_identity (needed by population CTE model/year filter)
    # Column order must match fi_quality_athena._VEHICLE_IDENTITY_DDL for compatible inserts.
    con.execute(f"CREATE SCHEMA IF NOT EXISTS adp_{stage}_vehicle_identity")
    con.execute(
        f"CREATE TABLE IF NOT EXISTS "
        f"adp_{stage}_vehicle_identity.vehicle_identity ("
        f'"vin" VARCHAR, "model" VARCHAR, "model_year" INTEGER, '
        f'"manufacture_date" DATE, "assembly_plant" VARCHAR'
        f")"
    )

    # service_records — add service_id column if absent (remedied_adp CTE needs it).
    # fi_rw_world creates this table without service_id.
    # Create it first if it doesn't exist (fi_quality_athena.seed_fi_quality_tables
    # may not have been called yet when _refresh_lake triggers register_adp_views).
    con.execute(
        f"CREATE SCHEMA IF NOT EXISTS adp_{stage}_service_records"
    )
    con.execute(
        f"CREATE TABLE IF NOT EXISTS adp_{stage}_service_records.service_records ("
        f'"vin" VARCHAR, "service_date" DATE, "service_type" VARCHAR, '
        f'"dtc_codes" VARCHAR[], "total_cost_usd" DOUBLE, "outcome" VARCHAR, '
        f'"linked_campaign_id" VARCHAR, "service_id" VARCHAR'
        f")"
    )
    try:
        con.execute(
            f"ALTER TABLE adp_{stage}_service_records.service_records "
            f"ADD COLUMN IF NOT EXISTS service_id VARCHAR"
        )
    except Exception:  # noqa: BLE001
        pass  # DuckDB may reject ADD COLUMN IF NOT EXISTS if the column already exists

    # ── 4. Register VIEWs (drop existing data tables first) ───────────────
    # fi_rw_world._refresh_lake creates recall_vehicle_status and possibly
    # recall_campaigns_received as data tables.  DROP TABLE IF EXISTS lets
    # us register them as VIEWs instead.
    # Important: use information_schema to determine the type before dropping,
    # since DuckDB errors when using the wrong DROP type.

    def _drop_any(schema: str, name: str) -> None:
        """Drop *schema.name* whether it is a TABLE or a VIEW."""
        row = con.execute(
            "SELECT table_type FROM information_schema.tables "
            f"WHERE table_schema='{schema}' AND table_name='{name}'"
        ).fetchone()
        if row is None:
            return
        if row[0] == "VIEW":
            con.execute(f"DROP VIEW {schema}.{name}")
        else:
            con.execute(f"DROP TABLE {schema}.{name}")

    # recall_campaigns_received VIEW
    _drop_any(f"adp_{stage}_fleet_recall", "recall_campaigns_received")
    received_view = _extract_view_body(
        received_sql,
        f"adp_{stage}_fleet_recall.recall_campaigns_received",
    )
    con.execute(received_view)

    # recall_vehicle_status VIEW
    _drop_any(f"adp_{stage}_fleet_recall", "recall_vehicle_status")
    status_view = _extract_view_body(
        status_sql,
        f"adp_{stage}_fleet_recall.recall_vehicle_status",
    )
    con.execute(status_view)


def lot_rule_query(*, stage: str = "staging") -> str:
    """Return a DuckDB SELECT for the lot-rule population as (campaign_id, vin) pairs.

    Derived from the lot-rule CTEs in recall_vehicle_status.sql (ADP 9bb82e96:88–116).
    Used by tests that need to check the lot rule directly without running the full view.
    """
    sql_raw = _read_sql_from_git(_STATUS_SQL_RELPATH)
    sql = _translate_for_duckdb(sql_raw, stage=stage)
    # Extract issued_lots / lot_campaigns / lot_vins CTEs and wrap in a SELECT.
    # Find the CTEs between 'issued_lots AS (' and 'lot_vins AS (' ... 'SELECT …'
    m = re.search(
        r"(issued_lots\s+AS\s*\(.*?lot_vins\s+AS\s*\([^)]+\))",
        sql, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        raise ValueError("adp_views: could not locate lot-rule CTEs in SQL")
    return f"WITH\n{m.group(1)}\nSELECT campaign_id, vin FROM lot_vins ORDER BY campaign_id, vin"


def run_lot_rule(athena: Any, *, stage: str = "staging") -> list[tuple[str, str]]:
    """Run the lot-rule CTEs directly; return (campaign_id, vin) pairs."""
    rows = athena.con.execute(lot_rule_query(stage=stage)).fetchall()
    return [(r[0], r[1]) for r in rows]
