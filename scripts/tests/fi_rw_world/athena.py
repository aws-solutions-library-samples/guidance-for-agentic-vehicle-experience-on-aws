"""Athena served by DuckDB: the caller's SQL really runs, over tables typed from Glue.

Column names and types come from ``glue get-table`` of the live views and DMS lake
tables (read 2026-10-10, ``data/glue.json``). Results are rendered the way Athena
renders them in ``GetQueryResults``: every value a ``VarCharValue`` string, a header
row first on page 1 only, ``NULL`` as a ``Data`` entry with no ``VarCharValue``,
booleans ``true``/``false``, arrays ``[a, b]`` (as the live recall evidence shows:
``"plants": "[CGA]"``), pages of at most 1000 rows with ``NextToken``.

``current_date`` is replaced by the world clock's date before the SQL runs, so a
query sees the same day as the rest of the world.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import uuid
from typing import Any, Callable

import duckdb

from scripts.tests.fi_rw_world.awsfake import AwsError, Router

_DUCK_TYPES = {
    "string": "VARCHAR", "boolean": "BOOLEAN", "date": "DATE", "int": "INTEGER",
    "bigint": "BIGINT", "double": "DOUBLE", "array<string>": "VARCHAR[]",
}


def duck_type(glue_type: str) -> str:
    if glue_type.startswith("varchar"):
        return "VARCHAR"
    if glue_type.startswith("decimal"):
        return glue_type.upper()
    return _DUCK_TYPES[glue_type]


def render(v: Any) -> dict[str, str]:
    if v is None:
        return {}
    if isinstance(v, bool):
        return {"VarCharValue": "true" if v else "false"}
    if isinstance(v, (list, tuple)):
        return {"VarCharValue": "[" + ", ".join("" if x is None else str(x) for x in v) + "]"}
    if isinstance(v, (_dt.date, _dt.datetime)):
        return {"VarCharValue": v.isoformat()}
    if isinstance(v, decimal.Decimal):
        return {"VarCharValue": format(v, "f")}
    return {"VarCharValue": str(v)}


class FakeAthena:
    def __init__(self, clock: Any, workgroups: set[str]) -> None:
        self.clock = clock
        self.con = duckdb.connect()
        self.workgroups = workgroups
        self.executions: dict[str, dict[str, Any]] = {}
        self.queries: list[str] = []
        self.before_query: list[Callable[[], None]] = []

    # -- tables ----------------------------------------------------------
    def create_table(self, qualified: str, columns: list[tuple[str, str]],
                     rows: list[dict[str, Any]]) -> None:
        schema, name = qualified.split(".")
        self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        cols = ", ".join(f'"{c}" {duck_type(t)}' for c, t in columns)
        self.con.execute(f"CREATE OR REPLACE TABLE {schema}.{name} ({cols})")
        if rows:
            names = [c for c, _ in columns]
            ph = ", ".join("?" for _ in names)
            self.con.executemany(
                f"INSERT INTO {schema}.{name} VALUES ({ph})",
                [[r.get(n) for n in names] for r in rows])

    # -- API -------------------------------------------------------------
    def start_query_execution(self, p: dict[str, Any]) -> dict[str, Any]:
        wg = p.get("WorkGroup") or "primary"
        if wg not in self.workgroups:
            raise AwsError("InvalidRequestException", f"WorkGroup {wg} is not found.")
        sql = p["QueryString"]
        self.queries.append(sql)
        eid = str(uuid.uuid4())
        ex: dict[str, Any] = {
            "QueryExecutionId": eid, "Query": sql, "WorkGroup": wg,
            "StatementType": "DML",
            "ResultConfiguration": {"OutputLocation": f"s3://world-athena-results/{eid}.csv"},
            "Status": {"State": "SUCCEEDED",
                       "SubmissionDateTime": self.clock.now(),
                       "CompletionDateTime": self.clock.now()},
        }
        for hook in self.before_query:
            hook()
        try:
            day = self.clock.now().date().isoformat()
            run_sql = sql.replace("current_date", f"DATE '{day}'").replace(
                "CURRENT_DATE", f"DATE '{day}'")
            db = (p.get("QueryExecutionContext") or {}).get("Database")
            ex["QueryExecutionContext"] = {"Database": db} if db else {}
            self.con.execute(f"SET search_path = '{db},main'" if db else "SET search_path = 'main'")
            cur = self.con.execute(run_sql)
            ex["_cols"] = [d[0] for d in cur.description or []]
            ex["_types"] = [str(d[1]) for d in cur.description or []]
            ex["_rows"] = cur.fetchall()
        except Exception as exc:  # noqa: BLE001 — Athena reports the failure in Status
            ex["Status"] = {"State": "FAILED", "StateChangeReason": f"{type(exc).__name__}: {exc}"}
        self.executions[eid] = ex
        return {"QueryExecutionId": eid}

    def get_query_execution(self, p: dict[str, Any]) -> dict[str, Any]:
        ex = self.executions.get(p["QueryExecutionId"])
        if ex is None:
            raise AwsError("InvalidRequestException", "QueryExecution not found")
        return {"QueryExecution": {k: v for k, v in ex.items() if not k.startswith("_")}}

    def get_query_results(self, p: dict[str, Any]) -> dict[str, Any]:
        ex = self.executions.get(p["QueryExecutionId"])
        if ex is None:
            raise AwsError("InvalidRequestException", "QueryExecution not found")
        if ex["Status"]["State"] != "SUCCEEDED":
            raise AwsError("InvalidRequestException",
                           f"Query has not yet finished. Current state: {ex['Status']['State']}")
        max_results = min(int(p.get("MaxResults") or 1000), 1000)
        start = int(p.get("NextToken") or 0)
        rows: list[dict[str, Any]] = []
        if start == 0:
            rows.append({"Data": [{"VarCharValue": c} for c in ex["_cols"]]})
        body = ex["_rows"][start:start + max_results - (1 if start == 0 else 0)]
        rows += [{"Data": [render(v) for v in r]} for r in body]
        consumed = start + len(body)
        out: dict[str, Any] = {
            "ResultSet": {"Rows": rows, "ResultSetMetadata": {"ColumnInfo": [
                {"CatalogName": "hive", "SchemaName": "", "TableName": "", "Name": c,
                 "Label": c, "Type": t.lower(), "Precision": 0, "Scale": 0,
                 "Nullable": "UNKNOWN", "CaseSensitive": True}
                for c, t in zip(ex["_cols"], ex["_types"])]}},
            "UpdateCount": 0,
        }
        if consumed < len(ex["_rows"]):
            out["NextToken"] = str(consumed)
        return out

    def register(self, router: Router) -> None:
        router.add("athena", "StartQueryExecution", self.start_query_execution)
        router.add("athena", "GetQueryExecution", self.get_query_execution)
        router.add("athena", "GetQueryResults", self.get_query_results)
