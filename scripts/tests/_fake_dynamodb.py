# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A small DynamoDB JSON-protocol endpoint on 127.0.0.1, for tests only (stdlib only).

Used by the re-VIN scrub tests (ADP spec ``2026-09-30-cms-adp-convergence-stage1``) so
the scripts run end to end through real botocore -- signing, retries, error parsing,
pagination -- without reaching AWS. Point a client at it with ``AWS_ENDPOINT_URL``.

What it models, and why each part is here:

* ``page_size`` items are *evaluated* per Query/Scan page, before the filter, and a
  ``LastEvaluatedKey`` is returned while items remain. A filtered page can therefore
  come back empty with a next key, which is the case first-page-only readers get wrong.
* ``ConditionExpression`` on ``UpdateItem`` is evaluated; a failure is a real
  ``ConditionalCheckFailedException``.
* ``stale_index_reads``: after the first write, that many index (GSI) query pages are
  answered from the state before the write, which is how eventual consistency looks to
  a caller that counts on an index right after updating. ``stale_index_name`` limits
  this to one index.
* As DynamoDB does, a request is refused (``ValidationException``) if it supplies an
  expression attribute name or value that no expression uses, or asks for
  ``ConsistentRead`` on a global secondary index.
* ``GetItem`` returns the item (projected) or no ``Item`` key on a miss.
* A request naming a table the fake does not hold gets ``ResourceNotFoundException``.
* Every request is recorded in ``calls`` as ``(operation, body)``.

Only the expression forms the scrub scripts use are supported; anything else raises, so
a test fails loudly instead of passing on a misread expression.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

READ_OPERATIONS = frozenset({
    "Query", "Scan", "GetItem", "BatchGetItem", "DescribeTable", "ListTables",
})


class _ExprError(ValueError):
    pass


class _NotFound(LookupError):
    pass


def _name(token: str, names: dict[str, str]) -> str:
    token = token.strip()
    if token.startswith("#"):
        if token not in names:
            raise _ExprError(f"undefined attribute name {token}")
        return names[token]
    return token


def _path(expr: str, names: dict[str, str]) -> list[str]:
    return [_name(part, names) for part in expr.strip().split(".")]


def _get(item: dict[str, Any], path: list[str]) -> Any:
    cur: Any = item
    for i, part in enumerate(path):
        if i > 0:
            if not isinstance(cur, dict) or "M" not in cur:
                return None
            cur = cur["M"]
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(item: dict[str, Any], path: list[str], value: Any) -> None:
    cur = item
    for part in path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict) or "M" not in nxt:
            raise _ExprError(f"document path {'.'.join(path)} does not exist")
        cur = nxt["M"]
    cur[path[-1]] = value


def _scalar(value: Any) -> Any:
    if not isinstance(value, dict) or len(value) != 1:
        return value
    (kind, raw), = value.items()
    if kind == "N":
        return float(raw)
    return raw


_CMP = re.compile(r"^(?P<lhs>[#\w.]+)\s*(?P<op><>|<=|>=|=|<|>)\s*(?P<rhs>:\w+)$")
_IN = re.compile(r"^(?P<lhs>[#\w.]+)\s+IN\s*\((?P<rhs>[^)]*)\)$")
_FN = re.compile(r"^(?P<fn>attribute_exists|attribute_not_exists)\((?P<arg>[#\w.]+)\)$")


def _eval(expr: str | None, item: dict[str, Any], names: dict[str, str],
          values: dict[str, Any]) -> bool:
    if not expr:
        return True
    for clause in expr.split(" AND "):
        clause = clause.strip()
        if m := _CMP.match(clause):
            left = _get(item, _path(m["lhs"], names))
            if m["rhs"] not in values:
                raise _ExprError(f"undefined value {m['rhs']}")
            right = values[m["rhs"]]
            op = m["op"]
            if left is None:
                ok = op == "<>"
            elif op == "=":
                ok = left == right
            elif op == "<>":
                ok = left != right
            else:
                lv, rv = _scalar(left), _scalar(right)
                ok = {"<": lv < rv, ">": lv > rv, "<=": lv <= rv, ">=": lv >= rv}[op]
        elif m := _IN.match(clause):
            left = _get(item, _path(m["lhs"], names))
            opts = [values[t.strip()] for t in m["rhs"].split(",") if t.strip()]
            ok = left is not None and left in opts
        elif m := _FN.match(clause):
            present = _get(item, _path(m["arg"], names)) is not None
            ok = present if m["fn"] == "attribute_exists" else not present
        else:
            raise _ExprError(f"unsupported expression clause: {clause!r}")
        if not ok:
            return False
    return True


_EXPRESSION_FIELDS = ("KeyConditionExpression", "FilterExpression", "ConditionExpression",
                      "UpdateExpression", "ProjectionExpression")


def _check_unused(body: dict[str, Any]) -> None:
    text = " ".join(str(body.get(f, "")) for f in _EXPRESSION_FIELDS)
    used = set(re.findall(r"[#:]\w+", text))
    for field_name in ("ExpressionAttributeNames", "ExpressionAttributeValues"):
        unused = set(body.get(field_name, {})) - used
        if unused:
            raise _ExprError(
                f"Value provided in {field_name} unused in expressions: keys: {sorted(unused)}"
            )


class FakeDynamoDB:
    """In-memory tables served over HTTP.

    ``tables`` maps a table name to ``{"key": [attr, ...], "indexes": {name: attrs},
    "items": [ddb-typed item, ...]}``, where ``attrs`` is the index's hash attribute or a
    ``(hash, range)`` pair. An item missing any index key attribute is absent from that
    index, as in DynamoDB.
    """

    def __init__(self, tables: dict[str, dict[str, Any]], *, page_size: int = 1,
                 stale_index_reads: int = 0, stale_index_name: str | None = None) -> None:
        self.tables = tables
        self.page_size = page_size
        self.stale_index_reads = stale_index_reads
        self.stale_index_name = stale_index_name
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._snapshot: dict[str, dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> str:
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # keep test output quiet
                return

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                target = self.headers.get("X-Amz-Target", "")
                op = target.split(".", 1)[-1]
                status, payload = fake._dispatch(op, body)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/x-amz-json-1.0")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- inspection ------------------------------------------------------------------
    def operations(self) -> list[str]:
        return [op for op, _ in self.calls]

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(op, body) for op, body in self.calls if op not in READ_OPERATIONS]

    def items(self, table: str) -> list[dict[str, Any]]:
        return self.tables[table]["items"]

    # -- dispatch --------------------------------------------------------------------
    def _dispatch(self, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._lock:
            self.calls.append((op, copy.deepcopy(body)))
            try:
                _check_unused(body)
                if op == "GetItem":
                    return 200, self._get(body)
                if op == "Query":
                    return 200, self._read(body, index=True)
                if op == "Scan":
                    return 200, self._read(body, index=False)
                if op == "UpdateItem":
                    return self._update(body)
                return 400, _error("UnknownOperationException", f"fake does not serve {op}")
            except _ExprError as exc:
                return 400, _error("ValidationException", str(exc))
            except _NotFound as exc:
                return 400, _error("ResourceNotFoundException", str(exc))

    def _source(self, table: str, use_snapshot: bool) -> list[dict[str, Any]]:
        if use_snapshot and self._snapshot is not None:
            return self._snapshot[table]["items"]
        return self.tables[table]["items"]

    def _read(self, body: dict[str, Any], *, index: bool) -> dict[str, Any]:
        table = body["TableName"]
        if table not in self.tables:
            raise _NotFound(f"Requested resource not found: {table}")
        names = body.get("ExpressionAttributeNames", {})
        values = body.get("ExpressionAttributeValues", {})
        stale = False
        if index and body.get("IndexName"):
            if body.get("ConsistentRead"):
                raise _ExprError("Consistent reads are not supported on global secondary indexes")
            attrs = self.tables[table]["indexes"].get(body["IndexName"])
            if attrs is None:
                raise _ExprError(f"no index {body['IndexName']} on {table}")
            index_attrs = (attrs,) if isinstance(attrs, str) else tuple(attrs)
            if (self._snapshot is not None and self.stale_index_reads > 0
                    and self.stale_index_name in (None, body["IndexName"])):
                self.stale_index_reads -= 1
                stale = True
            pool = [i for i in self._source(table, stale) if all(a in i for a in index_attrs)]
        else:
            pool = self._source(table, False)
        start = int(body.get("ExclusiveStartKey", {}).get("__i", {}).get("N", "0"))
        page = pool[start:start + self.page_size]
        matched = []
        for item in page:
            if index and not _eval(body.get("KeyConditionExpression"), item, names, values):
                continue
            if not _eval(body.get("FilterExpression"), item, names, values):
                continue
            matched.append(item)
        resp: dict[str, Any] = {"Count": len(matched), "ScannedCount": len(page)}
        if body.get("Select") != "COUNT":
            proj = body.get("ProjectionExpression")
            if proj:
                attrs = [_name(t, names) for t in proj.split(",")]
                matched = [{a: i[a] for a in attrs if a in i} for i in matched]
            resp["Items"] = copy.deepcopy(matched)
        if start + self.page_size < len(pool):
            resp["LastEvaluatedKey"] = {"__i": {"N": str(start + self.page_size)}}
        return resp

    def _get(self, body: dict[str, Any]) -> dict[str, Any]:
        table = body["TableName"]
        if table not in self.tables:
            raise _NotFound(f"Requested resource not found: {table}")
        spec = self.tables[table]
        key = body["Key"]
        if sorted(key) != sorted(spec["key"]):
            raise _ExprError("The provided key element does not match the schema")
        found = next(
            (i for i in spec["items"] if all(i.get(k) == v for k, v in key.items())), None
        )
        if found is None:
            return {}
        proj = body.get("ProjectionExpression")
        if proj:
            names = body.get("ExpressionAttributeNames", {})
            attrs = [_name(t, names) for t in proj.split(",")]
            found = {a: found[a] for a in attrs if a in found}
        return {"Item": copy.deepcopy(found)}

    def _update(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        table = body["TableName"]
        if table not in self.tables:
            raise _NotFound(f"Requested resource not found: {table}")
        spec = self.tables[table]
        key = body["Key"]
        if sorted(key) != sorted(spec["key"]):
            raise _ExprError("The provided key element does not match the schema")
        target = next(
            (i for i in spec["items"] if all(i.get(k) == v for k, v in key.items())), None
        )
        names = body.get("ExpressionAttributeNames", {})
        values = body.get("ExpressionAttributeValues", {})
        probe = target if target is not None else {}
        if not _eval(body.get("ConditionExpression"), probe, names, values):
            return 400, _error("ConditionalCheckFailedException",
                               "The conditional request failed")
        if target is None:
            target = copy.deepcopy(key)
            spec["items"].append(target)
        if self._snapshot is None:
            self._snapshot = copy.deepcopy(self.tables)
        expr = body["UpdateExpression"].strip()
        if not expr.startswith("SET "):
            raise _ExprError(f"unsupported update expression {expr!r}")
        for assignment in expr[4:].split(","):
            lhs, _, rhs = assignment.partition("=")
            rhs = rhs.strip()
            if rhs not in values:
                raise _ExprError(f"undefined value {rhs}")
            _set(target, _path(lhs, names), copy.deepcopy(values[rhs]))
        return 200, {}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"__type": f"com.amazonaws.dynamodb.v20120810#{code}", "message": message}
