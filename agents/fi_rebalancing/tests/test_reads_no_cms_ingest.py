"""Rule 3 for the rebalancing agent: its SQL reads only the databases it is allowed to.

The AVX Tier 2 role holds no ``cms_ingest`` grant (user rule, 2026-10-09; spec stage E, contract
§ 1a). ``agents/tier2/tests/test_stage_d_reads_no_cms_ingest.py`` scans the Python of every
module that calls ``run_query``, which includes ``sources.py``, but its SQL scan covers stage C's
folder only (security review cycle 1 W1). This test pins this agent's SQL files with an
allowlist of databases, not a denylist, so a new database of any name fails until it is added
here on purpose.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SQL_DIR = Path(__file__).parents[1] / "sql"

#: Every database the agent's SQL may read, with ``${stage}`` unexpanded.
_ALLOWED_DATABASES = {
    "adp_${stage}_fleet_rebalance",
    "adp_${stage}_service_records",
    "dms_${stage}",
}

# A qualified table after FROM or JOIN: <database>.<table>. Unqualified names are CTEs.
_QUALIFIED = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w${}]*)\s*\.\s*[A-Za-z_]\w*", re.IGNORECASE)

_SQL_FILES = sorted(_SQL_DIR.glob("*.sql"))


def test_the_sql_folder_is_the_one_the_agent_loads():
    """Anti-vacuity: the folder holds the agent's ten reads (sources.py loads from it)."""
    names = {p.name for p in _SQL_FILES}
    assert len(names) == 10, sorted(names)
    sources = (Path(__file__).parents[1] / "sources.py").read_text()
    for name in names:
        assert name in sources, f"{name} is not loaded by sources.py"


@pytest.mark.parametrize("sql_path", _SQL_FILES, ids=lambda p: p.name)
def test_sql_reads_only_allowed_databases(sql_path: Path):
    sql = sql_path.read_text()
    databases = {m.group(1) for m in _QUALIFIED.finditer(sql)}
    assert databases, f"{sql_path.name}: no qualified FROM/JOIN target found"
    assert databases <= _ALLOWED_DATABASES, (
        f"{sql_path.name} reads {sorted(databases - _ALLOWED_DATABASES)}; the AVX Tier 2 role "
        "may read only " + ", ".join(sorted(_ALLOWED_DATABASES))
    )


@pytest.mark.parametrize("sql_path", _SQL_FILES, ids=lambda p: p.name)
def test_sql_never_names_cms_ingest(sql_path: Path):
    """Catches a cms_ingest reference the FROM pattern might not parse (a comment is fine)."""
    code = "\n".join(line.split("--", 1)[0] for line in sql_path.read_text().splitlines())
    assert "cms_ingest" not in code.lower(), f"{sql_path.name} names cms_ingest"
