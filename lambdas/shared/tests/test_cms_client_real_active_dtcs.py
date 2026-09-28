"""Active-DTC read for the triage classifier: status decides, not row age.

Regression guard for issues/2026-09-25-triage-drops-active-dtcs-first-seen-over-24h/.
Triage returned P3 for VEH-MRDN-0015 while the iOS app showed an active P0217,
because get_active_dtcs bounded its query to rows first raised in the last 24h.

The DynamoDB stub below HONORS the key condition (hash equality and any sort-key
bound) and paginates. The old stub returned every seeded row whatever the query
said, so a time window in the query could never fail a test. The stub raises on
any key-condition shape it does not understand rather than ignoring it.

Deliberately does not import fakeredis (absent from .venv and CI); the DTC path
never touches Redis. Run:
    .venv/bin/python3 -m pytest lambdas/shared/tests/test_cms_client_real_active_dtcs.py -q

Mutations verified against this file (each makes at least one test fail):
  1. Restore `timestamp >= now - 24h` in the key condition.
  2. Drop the `clearedDate` exclusion.
  3. Read one page only (ignore LastEvaluatedKey).
  4. Return () when a later page fails.
  5. Restore the one-page `Limit=50` read.
Added after review cycle 1 (review-a137e13-cycle1.md W1-W3):
  6. Treat an absent `clearedDate` attribute as cleared (`get("clearedDate", "x")`).
  7. Delete the empty-code guard.
  8. Report `complete` on a first-page failure.
  9. Report `complete` (or `failed`) on a later-page failure.
 10. Never set `last_dtc_read` on the unresolved-VIN path.
Added after review cycle 2 (review-a137e13-cycle2.md W5):
 11. Record a vehicles-table failure as `vehicle_unresolved`.
 12. Never reset the lookup-failure flag between calls.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve()
LAMBDAS = HERE.parents[2]
sys.path.insert(0, str(LAMBDAS))


def _register_triage_classifier_package() -> None:
    """The classifier source lives in lambdas/triage-classifier/ (hyphen), and the
    bundle renames it to triage_classifier (scripts/build-triage-classifier.sh).
    Mirror that rename so `shared.cms_client_real` imports as it does in Lambda."""
    if "triage_classifier" in sys.modules:
        return
    pkg_dir = LAMBDAS / "triage-classifier"
    spec = importlib.util.spec_from_file_location(
        "triage_classifier", pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["triage_classifier"] = module
    spec.loader.exec_module(module)


_register_triage_classifier_package()

from boto3.dynamodb.conditions import ConditionExpressionBuilder  # noqa: E402

from shared.cms_client_real import RealCmsClient  # noqa: E402
from triage_classifier.classifier import classify  # noqa: E402
from triage_classifier.types import (  # noqa: E402
    Level,
    TelemetrySnapshot,
    TenantThresholds,
    TenantTriageConfig,
    TriageContext,
)

VIN = "MRDN0000000000015"
VEHICLE_ID = "VEH-MRDN-0015"
VEHICLES = "vehicles"
DTCS = "dtc-history"
HOUR_MS = 3600 * 1000


# ---- DynamoDB stub that honors the query ------------------------------------


class _StubTable:
    """Query honors hash equality, sort-key bounds, ScanIndexForward, Limit and
    pagination, for both string and boto3-condition key expressions."""

    _TERM = re.compile(r"^\s*(\S+)\s*(=|>=|<=|>|<)\s*(:\S+)\s*$")

    def __init__(self, items: list[dict], page_size: int | None = None,
                 fail_on_page: int | None = None) -> None:
        self._items = items
        self._page_size = page_size
        self._fail_on_page = fail_on_page
        self.pages_served = 0

    def _render(self, kwargs: dict) -> tuple[str, dict, dict]:
        expr = kwargs["KeyConditionExpression"]
        names = dict(kwargs.get("ExpressionAttributeNames") or {})
        values = dict(kwargs.get("ExpressionAttributeValues") or {})
        if not isinstance(expr, str):
            built = ConditionExpressionBuilder().build_expression(expr, is_key_condition=True)
            expr = built.condition_expression
            names.update(built.attribute_name_placeholders)
            values.update(built.attribute_value_placeholders)
        return expr, names, values

    def query(self, **kwargs: Any) -> dict:
        page = self.pages_served
        self.pages_served += 1
        if self._fail_on_page is not None and page == self._fail_on_page:
            raise RuntimeError("simulated throttle")
        if "IndexName" in kwargs:  # vin-index lookup
            vin = kwargs["ExpressionAttributeValues"][":v"]
            return {"Items": [i for i in self._items if i.get("vin") == vin][:1]}

        expr, names, values = self._render(kwargs)
        # boto3 renders "(#n0 = :v0 AND #n1 >= :v1)"; only AND is supported.
        expr = expr.replace("(", " ").replace(")", " ")
        rows = list(self._items)
        for term in re.split(r"\s+AND\s+", expr, flags=re.IGNORECASE):
            m = self._TERM.match(term)
            if not m:
                raise AssertionError(f"stub does not understand key condition term {term!r}")
            attr, op, ph = m.groups()
            attr = names.get(attr, attr)
            want = values[ph]
            ops = {"=": lambda a, b: a == b, ">=": lambda a, b: a >= b,
                   "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
                   "<": lambda a, b: a < b}
            rows = [r for r in rows if attr in r and ops[op](r[attr], want)]
        rows.sort(key=lambda r: r["timestamp"], reverse=not kwargs.get("ScanIndexForward", True))

        start = kwargs.get("ExclusiveStartKey")
        if start:
            idx = next(i for i, r in enumerate(rows) if r["timestamp"] == start["timestamp"])
            rows = rows[idx + 1:]
        cap = min(x for x in (kwargs.get("Limit"), self._page_size, len(rows)) if x is not None)
        out = rows[:cap]
        resp: dict[str, Any] = {"Items": out}
        if len(rows) > cap and out:
            resp["LastEvaluatedKey"] = {"vehicleId": out[-1]["vehicleId"],
                                        "timestamp": out[-1]["timestamp"]}
        return resp


class _StubDdb:
    def __init__(self, tables: dict[str, _StubTable]) -> None:
        self._tables = tables

    def Table(self, name: str) -> _StubTable:  # noqa: N802 - boto3 shape
        return self._tables[name]


def _row(code: str, first_seen_hours_ago: float, *, status: str = "ACTIVE",
         cleared_date: str | None = "", severity: str = "CRITICAL",
         last_seen_hours_ago: float | None = None) -> dict:
    """One dtc-history row. ``cleared_date=None`` omits the attribute entirely,
    which is how 42 of 178 active staging rows look (review W1): an absent
    ``clearedDate`` is not the same shape as an empty one."""
    now = int(time.time() * 1000)
    ts = now - int(first_seen_hours_ago * HOUR_MS)
    row = {
        "vehicleId": VEHICLE_ID, "timestamp": ts, "code": code, "status": status,
        "severity": severity, "firstSeenAt": str(ts), "persistent": True,
    }
    if cleared_date is not None:
        row["clearedDate"] = cleared_date
    if last_seen_hours_ago is not None:
        row["lastSeenAt"] = str(now - int(last_seen_hours_ago * HOUR_MS))
    return row


def _no_dtc_row(first_seen_hours_ago: float) -> dict:
    """A code-less ``ACTIVE_NO_DTC`` row, as CMS's MaintenanceProcessor
    ``writeDtcHistoryRow(vehicleId, "", "ACTIVE_NO_DTC", ...)`` writes it
    (review W2). It is not a fault and must never reach the classifier."""
    return _row("", first_seen_hours_ago, status="ACTIVE_NO_DTC", severity="LOW")


def _client(dtc_rows: list[dict], **table_kwargs: Any) -> tuple[RealCmsClient, _StubTable]:
    dtc_table = _StubTable(dtc_rows, **table_kwargs)
    ddb = _StubDdb({
        VEHICLES: _StubTable([{"vehicleId": VEHICLE_ID, "vin": VIN, "timestamp": 0}]),
        DTCS: dtc_table,
    })
    return RealCmsClient(redis_client=None, ddb_resource=ddb), dtc_table


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    monkeypatch.setenv("CMS_VEHICLES_TABLE", VEHICLES)
    monkeypatch.setenv("CMS_DTC_HISTORY_TABLE", DTCS)
    monkeypatch.delenv("CMS_REDIS_ENDPOINT", raising=False)


def _live_partition() -> list[dict]:
    """VEH-MRDN-0015's partition as read 2026-09-25, ages relative to 15:50Z:
    four faults first raised 2026-09-22T19:03Z (~69h) and still active, the
    same four codes cleared on 2026-08-19, and two legacy ACTIVE rows. The real
    B1234 row carries no clearedDate attribute at all (review W1)."""
    return [
        _row("P0562", 68.8, severity="LOW", last_seen_hours_ago=3.6),
        _row("P0001", 68.8, severity="MEDIUM", last_seen_hours_ago=3.6),
        _row("P0299", 68.9, severity="HIGH", last_seen_hours_ago=3.6),
        _row("P0217", 68.95, severity="CRITICAL", last_seen_hours_ago=3.6),
        _row("P0217", 885, status="CLEARED", cleared_date="2026-08-19T20:59:21Z"),
        _row("B0001_FIRE", 885.1, status="CLEARED", cleared_date="2026-08-19T20:59:21Z"),
        _row("B1234", 1155, severity="MEDIUM", cleared_date=None),
        _row("P0420", 1392, severity="MEDIUM"),
        _row("U0100", 2091, status="CLEARED", cleared_date="2026-07-01", severity="LOW"),
    ]


# ---- Tests -------------------------------------------------------------------


def test_active_fault_first_raised_days_ago_is_returned():
    client, _ = _client([_row("P0217", 69, last_seen_hours_ago=3.5)])
    assert [d.code for d in client.get_active_dtcs(VIN)] == ["P0217"]


def test_live_partition_classifies_p0():
    """The 15:50Z case end to end: DTC read -> deterministic classifier -> P0.
    Telemetry is stale, as it is live (the classifier Lambda has no Redis)."""
    client, _ = _client(_live_partition())
    dtcs = client.get_active_dtcs(VIN)
    assert "P0217" in {d.code for d in dtcs}
    decision = classify(TriageContext(
        session_id="sess-test", tenant_id="meridian", tenant_segment="fleet", vin=VIN,
        active_dtcs=dtcs, telemetry=TelemetrySnapshot(stale=True), open_recalls=(),
        predictive_signals=(),
        tenant_triage=TenantTriageConfig(thresholds=TenantThresholds(), escalations=(),
                                         predictive_weight="none"),
    ))
    assert decision.classification == Level.P0


def test_cleared_resolved_and_cleared_date_rows_are_not_active():
    client, _ = _client([
        _row("C1111", 1, status="CLEARED"),
        _row("C2222", 2, status="RESOLVED"),
        _row("C3333", 3, status="ACTIVE", cleared_date="2026-09-01T00:00:00Z"),
        _row("C4444", 4),
        _row("C5555", 5, cleared_date=None),  # no clearedDate attribute: still active
        _no_dtc_row(6),                        # code-less ACTIVE_NO_DTC: not a fault
    ])
    assert [d.code for d in client.get_active_dtcs(VIN)] == ["C4444", "C5555"]


def test_active_row_without_cleared_date_attribute_counts_as_active():
    """W1: an absent clearedDate is the most common active shape on staging for
    CRITICAL rows. Treating absence as cleared would drop them from triage."""
    client, _ = _client([_row("P0217", 69, cleared_date=None)])
    assert [d.code for d in client.get_active_dtcs(VIN)] == ["P0217"]


def test_codeless_active_no_dtc_row_never_reaches_the_classifier():
    """W2: a vehicle whose only non-cleared row is ACTIVE_NO_DTC has no fault.
    Without the empty-code guard it becomes ActiveDtc(code="") and the
    classifier's default rule lifts it from P3 to P2."""
    client, _ = _client([_no_dtc_row(1), _row("P0217", 50, status="CLEARED")])
    dtcs = client.get_active_dtcs(VIN)
    assert dtcs == ()
    decision = classify(TriageContext(
        session_id="sess-test", tenant_id="meridian", tenant_segment="fleet", vin=VIN,
        active_dtcs=dtcs, telemetry=TelemetrySnapshot(stale=True), open_recalls=(),
        predictive_signals=(),
        tenant_triage=TenantTriageConfig(thresholds=TenantThresholds(), escalations=(),
                                         predictive_weight="none"),
    ))
    assert decision.classification == Level.P3


def test_newest_active_row_per_code_wins():
    newer = _row("P0217", 100, last_seen_hours_ago=1)
    older = _row("P0217", 200)
    client, _ = _client([older, newer])
    dtcs = client.get_active_dtcs(VIN)
    assert len(dtcs) == 1
    assert dtcs[0].first_seen_at == newer["firstSeenAt"]


def test_reads_every_page():
    rows = [_row(f"B{n:04d}", n, severity="LOW") for n in range(1, 8)]
    rows.append(_row("P0217", 500))  # oldest, so last page newest-first
    client, table = _client(rows, page_size=2)
    codes = {d.code for d in client.get_active_dtcs(VIN)}
    assert "P0217" in codes and len(codes) == 8
    assert table.pages_served == 4
    assert client.last_dtc_read == {"status": "complete", "pages": 4, "rows": 8}


def test_partition_larger_than_fifty_rows_is_read_in_full():
    rows = [_row(f"B{n:04d}", n, severity="LOW") for n in range(1, 61)]
    rows.append(_row("P0217", 999))
    client, _ = _client(rows)
    assert "P0217" in {d.code for d in client.get_active_dtcs(VIN)}


def test_later_page_failure_keeps_rows_already_read_and_reports_partial():
    rows = [_row("P0217", 1), _row("P0299", 2, severity="HIGH"),
            _row("P0420", 3, severity="MEDIUM"), _row("P0001", 4, severity="MEDIUM")]
    client, _ = _client(rows, page_size=2, fail_on_page=1)
    assert {d.code for d in client.get_active_dtcs(VIN)} == {"P0217", "P0299"}
    assert client.last_dtc_read == {"status": "partial", "pages": 1, "rows": 2}


def test_first_page_failure_returns_empty_and_reports_failed():
    """W3: () from a failed read must be distinguishable from () for a vehicle
    with no faults. The handler stores last_dtc_read in inputsSnapshot."""
    client, _ = _client([_row("P0217", 1)], fail_on_page=0)
    assert client.get_active_dtcs(VIN) == ()
    assert client.last_dtc_read == {"status": "failed", "pages": 0, "rows": 0}


def test_no_faults_is_a_complete_read_not_a_failure():
    client, _ = _client([_row("P0217", 1, status="CLEARED")])
    assert client.get_active_dtcs(VIN) == ()
    assert client.last_dtc_read == {"status": "complete", "pages": 1, "rows": 1}


def test_unresolved_vin_reports_vehicle_unresolved():
    client, table = _client([_row("P0217", 1)])
    assert client.get_active_dtcs("NOT-A-KNOWN-VIN00") == ()
    assert client.last_dtc_read == {"status": "vehicle_unresolved", "pages": 0, "rows": 0}
    assert table.pages_served == 0


class _FailingVehiclesTable:
    """The vehicles table when the role cannot read it: the vin-index query and
    the fallback scan both raise (IAM, KMS, throttling, wrong table name)."""

    def query(self, **kwargs: Any) -> dict:
        raise RuntimeError("simulated AccessDeniedException on vin-index")

    def scan(self, **kwargs: Any) -> dict:
        raise RuntimeError("simulated AccessDeniedException on scan")


def test_vehicle_lookup_failure_is_not_reported_as_unresolved():
    """W5 (review cycle 2): a vehicles-table outage fails here before any DTC
    query runs. Recording it as `vehicle_unresolved` would tell operators the VIN
    is missing from CMS, and would emit no degraded-read metric."""
    dtc_table = _StubTable([_row("P0217", 1)])
    client = RealCmsClient(redis_client=None, ddb_resource=_StubDdb({
        VEHICLES: _FailingVehiclesTable(), DTCS: dtc_table}))
    assert client.get_active_dtcs(VIN) == ()
    assert client.last_dtc_read == {"status": "vehicle_lookup_failed", "pages": 0, "rows": 0}
    assert dtc_table.pages_served == 0


def test_lookup_failure_flag_does_not_leak_into_the_next_call():
    """A warm Lambda reuses the client. After a failed lookup, a later VIN that is
    simply absent is `vehicle_unresolved`, not a stale failure."""
    tables = {VEHICLES: _FailingVehiclesTable(), DTCS: _StubTable([])}
    client = RealCmsClient(redis_client=None, ddb_resource=_StubDdb(tables))
    client.get_active_dtcs(VIN)
    assert client.last_dtc_read["status"] == "vehicle_lookup_failed"
    tables[VEHICLES] = _StubTable([{"vehicleId": VEHICLE_ID, "vin": VIN, "timestamp": 0}])
    client.get_active_dtcs("NOT-A-KNOWN-VIN00")
    assert client.last_dtc_read["status"] == "vehicle_unresolved"


def test_agrees_with_the_ios_app_reader(monkeypatch):
    """Triage and the iOS app must agree on which codes are active. The app's
    list comes from lambdas/api-vehicle-context/handler.py _load_active_dtcs."""
    path = LAMBDAS / "api-vehicle-context" / "handler.py"
    spec = importlib.util.spec_from_file_location("_avc_handler_parity", path)
    avc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(avc)

    rows = _live_partition() + [_no_dtc_row(0.5)]  # W2: parity over a code-less row too
    app_table = _StubTable(rows)
    monkeypatch.setattr(avc, "_get_ddb", lambda: _StubDdb({DTCS: app_table}))
    monkeypatch.setattr(avc, "_DTC_HISTORY_TABLE", DTCS)
    app_codes = {d["code"] for d in avc._load_active_dtcs(VEHICLE_ID)}

    client, _ = _client(rows)
    triage_codes = {d.code for d in client.get_active_dtcs(VIN)}

    assert app_codes == {"P0562", "P0001", "P0299", "P0217", "B1234", "P0420"}
    assert triage_codes == app_codes
