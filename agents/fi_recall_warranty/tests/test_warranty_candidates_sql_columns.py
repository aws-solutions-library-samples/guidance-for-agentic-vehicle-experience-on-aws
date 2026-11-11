"""The warranty candidates query reads only columns DMS's nightly job writes on every row.

``dms_{stage}.warranty_candidates`` is created by DMS's Glue merge job from the first batch of
rows it sees, and later merges drop columns the table lacks. Those rows come from DMS's
candidates job (``source/handlers/warranty_candidates.py``, the ``row`` dict), so a column only
the claim route writes (``claimed_at``) never reaches the lake, and selecting it fails
``COLUMN_NOT_FOUND`` (DMS issue ``2026-10-09-fi-lake-tables-have-no-merge-job``, review g5-dms W2).

Mutation (spec 2026-10-05-fi-recall-warranty-stage-c decisions.md): put ``claimed_at`` back in the
SELECT list; ``test_selects_only_columns_the_dms_job_always_writes`` fails naming it.
"""

from __future__ import annotations

import re
from pathlib import Path

_SQL = Path(__file__).resolve().parents[1] / "sql" / "select_warranty_candidates.sql"

#: Keys of the ``row`` dict DMS's candidates job writes for every candidate line, read from
#: DMS ``source/handlers/warranty_candidates.py`` at ``e94c615`` (unchanged on
#: ``wip/fi-rw-c-g5``). Update together with that file.
DMS_JOB_COLUMNS = frozenset({
    "ro_id", "line_no", "vehicle_vin", "dealer_id", "labor_op", "fault_code",
    "coverage_group", "in_service_date", "odometer_km", "repair_date", "verdict_status",
    "verdict_reason", "matched_rule_id", "recoverable_usd", "terms_version",
    "filing_deadline", "claimed", "computed_at",
})


def _select_list_sources(sql: str) -> list[str]:
    body = re.sub(r"--[^\n]*", "", sql)
    m = re.search(r"\bSELECT\b(.*?)\bFROM\b", body, re.S | re.I)
    assert m, "no SELECT ... FROM in the query"
    cols = []
    for item in (c.strip() for c in m.group(1).split(",")):
        cast = re.match(r"CAST\(\s*(\w+)\s+AS\s+\w+\s*\)\s+AS\s+\w+$", item, re.I)
        cols.append(cast.group(1) if cast else re.match(r"^(\w+)$", item).group(1))
    return cols


def test_selects_only_columns_the_dms_job_always_writes() -> None:
    cols = _select_list_sources(_SQL.read_text())
    assert len(cols) >= 15, cols
    assert sorted(set(cols) - DMS_JOB_COLUMNS) == [], (
        "selected columns the DMS candidates job does not write on every row")


def test_where_clause_reads_only_job_columns() -> None:
    body = re.sub(r"--[^\n]*", "", _SQL.read_text())
    where = re.search(r"\bWHERE\b(.*?)\bORDER BY\b", body, re.S | re.I).group(1)
    for col in ("claimed", "verdict_status", "filing_deadline"):
        assert re.search(rf"\b{col}\b", where), col
    assert "claimed_at" not in where
