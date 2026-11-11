"""A failed fault lookup must never answer as "no active faults".

`_load_active_dtcs` used to catch any dtc-history error and return `[]`, so the route answered
200 with `activeDtcs: []` and a healthy score, and iOS showed "No active faults — all clear."
for a read that never happened. issues/2026-09-27-vehicle-context-dtc-lookup-failure-returns-empty/.

The fix answers 503. That is the one shape every client already treats as a failure. iOS decodes
`activeDtcs` as optional and reads null as empty, so a 200 with `activeDtcs: null` would still say
"all clear". See the issue's 2026-10-01 investigation.

Tables are moto DynamoDB with the real key schemas (vehicleId/timestamp:N), so "failing table"
means a real client error from a real request path, not a mock that was told to fail.

Mutation ledger (2026-10-01, all CAUGHT):
  V1 restore the swallowed exception (`except ...: return []`)   -> failing-table tests FAIL
  V2 handler catches the error but answers 200 with activeDtcs null -> status/shape tests FAIL
  V3 handler answers 503 but still includes a healthScore          -> no-score test FAILS
  V4 lookup error raised for an EMPTY (successful) read             -> empty-history test FAILS
  V5 failure logged at WARNING instead of ERROR                    -> log-level test FAILS
  P1  read one page only                                         -> >200-row test FAILS
  P1b the exact pre-fix query (Limit=200, one page)               -> >200-row test FAILS
  P2  a failed later page returns the rows read so far            -> later-page test FAILS
  P3  no page cap                                                 -> cap test FAILS
  P4  ExclusiveStartKey not passed                                -> paging tests FAIL
  P5  oldest first                                                -> dedup test FAILS

Run:
    python3 -m pytest lambdas/api-vehicle-context/tests -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

REGION = "us-west-2"
TABLES = {
    "CMS_VEHICLES_TABLE": "t-vehicles",
    "CMS_DRIVERS_TABLE": "t-drivers",
    "CMS_DTC_HISTORY_TABLE": "t-dtc-history",
    "CMS_SERVICE_HISTORY_TABLE": "t-service-history",
}
VEHICLE = "VEH-TEST-0001"


def _load_handler():
    for k, v in TABLES.items():
        os.environ[k] = v
    os.environ.pop("LIVE_STATE_FN_NAME", None)  # no live-state invoke; DDB fallback
    path = Path(__file__).parent.parent / "handler.py"
    spec = importlib.util.spec_from_file_location("vehicle_context_handler_dtc_failure", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load_handler()


def _event(vehicle_id=VEHICLE):
    # A platform-admin caller: these tests are about the fault lookup, and the route
    # refuses a request with no caller scope (tests/test_context_scope.py).
    return {
        "pathParameters": {"vehicleId": vehicle_id},
        "requestContext": {"authorizer": {"claims": {
            "sub": "test-admin", "cognito:groups": "platform-admin",
        }}},
    }


def _body(resp):
    return json.loads(resp["body"])


@pytest.fixture
def ddb(monkeypatch):
    for var, val in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                     ("AWS_SESSION_TOKEN", "testing"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(var, val)
    with mock_aws():
        res = boto3.resource("dynamodb", region_name=REGION)

        def table(name, hash_key, range_key=None, range_type="S"):
            ks = [{"AttributeName": hash_key, "KeyType": "HASH"}]
            ad = [{"AttributeName": hash_key, "AttributeType": "S"}]
            if range_key:
                ks.append({"AttributeName": range_key, "KeyType": "RANGE"})
                ad.append({"AttributeName": range_key, "AttributeType": range_type})
            return res.create_table(TableName=name, KeySchema=ks, AttributeDefinitions=ad,
                                    BillingMode="PAY_PER_REQUEST")

        table(TABLES["CMS_VEHICLES_TABLE"], "vehicleId").put_item(Item={
            "vehicleId": VEHICLE, "vin": "MRDN0000000000099", "make": "Meridian",
            "connectionStatus": "connected",
        })
        table(TABLES["CMS_DRIVERS_TABLE"], "driverId")
        table(TABLES["CMS_SERVICE_HISTORY_TABLE"], "vehicleId", "serviceDate")
        # dtc-history is created per test, or deliberately not at all.
        monkeypatch.setattr(h, "_ddb", None)
        yield res


def _create_dtc_table(res):
    return res.create_table(
        TableName=TABLES["CMS_DTC_HISTORY_TABLE"],
        KeySchema=[{"AttributeName": "vehicleId", "KeyType": "HASH"},
                   {"AttributeName": "timestamp", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "vehicleId", "AttributeType": "S"},
                              {"AttributeName": "timestamp", "AttributeType": "N"}],
        BillingMode="PAY_PER_REQUEST",
    )


# --------------------------------------------------------------------------
# The property: a failed lookup is a failure response, never "no faults".
# --------------------------------------------------------------------------

def _assert_failure_response(resp):
    assert resp["statusCode"] == 503, (
        f"got {resp['statusCode']}: a failed fault lookup answered as success. iOS would show "
        "'No active faults — all clear.'"
    )
    body = _body(resp)
    assert body.get("dtcLookup") == "failed"
    assert "activeDtcs" not in body, "a failed lookup must not carry any fault list, even empty"
    assert "healthScore" not in body and "healthScoreBreakdown" not in body, (
        "a health score computed without the faults overstates health"
    )


def test_missing_dtc_table_is_503_not_all_clear(ddb):
    # Real failure from the real client: ResourceNotFoundException on Query.
    resp = h.handler(_event(), None)
    _assert_failure_response(resp)


@pytest.mark.parametrize("code", ["ProvisionedThroughputExceededException",
                                  "AccessDeniedException", "InternalServerError"])
def test_throttled_or_denied_dtc_query_is_503(ddb, monkeypatch, code):
    real_resource = h._get_ddb()

    class _FailingDtcTable:
        def query(self, **_kw):
            raise ClientError({"Error": {"Code": code, "Message": "simulated"}}, "Query")

    class _Resource:
        def Table(self, name):  # noqa: N802 — boto3 API name
            if name == TABLES["CMS_DTC_HISTORY_TABLE"]:
                return _FailingDtcTable()
            return real_resource.Table(name)

    monkeypatch.setattr(h, "_get_ddb", lambda: _Resource())
    resp = h.handler(_event(), None)
    _assert_failure_response(resp)


def test_load_active_dtcs_raises_rather_than_returning_empty(ddb):
    with pytest.raises(h.DtcLookupError):
        h._load_active_dtcs(VEHICLE)


def test_failure_is_logged_at_error(ddb, caplog):
    with caplog.at_level("INFO"):
        h.handler(_event(), None)
    hits = [r for r in caplog.records if "dtc lookup failed" in r.getMessage()]
    assert hits and all(r.levelname == "ERROR" for r in hits)
    assert all("\n" not in r.getMessage() for r in hits)


# --------------------------------------------------------------------------
# The normal path is unchanged: faults are returned, and "none" still means none.
# --------------------------------------------------------------------------

def test_normal_path_returns_active_faults(ddb):
    t = _create_dtc_table(ddb)
    now = int(time.time() * 1000)
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now, "code": "P0299",
                     "severity": "HIGH", "status": "ACTIVE", "dtcId": "d1"})
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now - 1000, "code": "P0299",
                     "severity": "HIGH", "status": "ACTIVE", "dtcId": "d0"})  # older duplicate
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now - 2000, "code": "C1234",
                     "severity": "CRITICAL", "status": "CLEARED"})

    resp = h.handler(_event(), None)

    assert resp["statusCode"] == 200
    body = _body(resp)
    assert [d["code"] for d in body["activeDtcs"]] == ["P0299"]
    assert body["activeDtcs"][0]["dtcId"] == "d1"
    assert "DTC P0299 HIGH" in [d["reason"] for d in body["healthScoreBreakdown"]["deductions"]]
    assert body["healthScore"] < 100


def test_empty_history_is_200_with_empty_list(ddb):
    # Distinguishes "read succeeded, nothing active" from "read failed".
    _create_dtc_table(ddb)
    resp = h.handler(_event(), None)
    assert resp["statusCode"] == 200
    assert _body(resp)["activeDtcs"] == []


def test_unknown_vehicle_is_still_404(ddb):
    _create_dtc_table(ddb)
    assert h.handler(_event("VEH-NOPE"), None)["statusCode"] == 404



# --------------------------------------------------------------------------
# Paging: every row is read. The old single `Limit=200` query dropped older
# still-ACTIVE faults past row 200 — the same false "no faults", silently.
# --------------------------------------------------------------------------

def _put_rows(table, rows):
    with table.batch_writer() as bw:
        for r in rows:
            bw.put_item(Item=r)


def test_fault_older_than_200_rows_is_returned(ddb):
    t = _create_dtc_table(ddb)
    now = int(time.time() * 1000)
    # 249 newer CLEARED rows for another code, then one ACTIVE fault as the 250th-newest row.
    rows = [{"vehicleId": VEHICLE, "timestamp": now - i, "code": "B1000",
             "status": "CLEARED", "dtcId": f"c{i}"} for i in range(249)]
    rows.append({"vehicleId": VEHICLE, "timestamp": now - 10_000, "code": "P0420",
                 "severity": "HIGH", "status": "ACTIVE", "dtcId": "old-active"})
    _put_rows(t, rows)
    assert h._DTC_PAGE_SIZE < 250, "the fixture must span more than one page"

    resp = h.handler(_event(), None)

    assert resp["statusCode"] == 200
    codes = [d["code"] for d in _body(resp)["activeDtcs"]]
    assert codes == ["P0420"], (
        f"got {codes}: an ACTIVE fault 250 rows back was dropped, so the vehicle read as having "
        "no faults"
    )


def test_dedup_keeps_the_newest_row_across_pages(ddb):
    t = _create_dtc_table(ddb)
    now = int(time.time() * 1000)
    rows = [{"vehicleId": VEHICLE, "timestamp": now - i, "code": "B1000",
             "status": "CLEARED", "dtcId": f"c{i}"} for i in range(1, 220)]
    rows.append({"vehicleId": VEHICLE, "timestamp": now, "code": "P0299",
                 "severity": "HIGH", "status": "ACTIVE", "dtcId": "newest"})
    rows.append({"vehicleId": VEHICLE, "timestamp": now - 500, "code": "P0299",
                 "severity": "LOW", "status": "ACTIVE", "dtcId": "older-dup"})
    _put_rows(t, rows)

    active = _body(h.handler(_event(), None))["activeDtcs"]

    assert [(d["code"], d["dtcId"]) for d in active] == [("P0299", "newest")]


class _PagedStub:
    """Reads succeed page by page; behaviour of later pages is set per test."""

    def __init__(self, later):
        self.calls = 0
        self._later = later

    def query(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return {"Items": [{"vehicleId": VEHICLE, "timestamp": 1, "code": "P0001",
                               "status": "ACTIVE"}],
                    "LastEvaluatedKey": {"vehicleId": VEHICLE, "timestamp": 1}}
        return self._later(self, kw)


def _with_dtc_table(monkeypatch, stub):
    real_resource = h._get_ddb()

    class _Resource:
        def Table(self, name):  # noqa: N802
            return stub if name == TABLES["CMS_DTC_HISTORY_TABLE"] else real_resource.Table(name)

    monkeypatch.setattr(h, "_get_ddb", lambda: _Resource())


def test_error_on_a_later_page_is_503_not_a_partial_list(ddb, monkeypatch):
    def throttle(_self, _kw):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query")

    _with_dtc_table(monkeypatch, _PagedStub(throttle))
    _assert_failure_response(h.handler(_event(), None))


def test_next_page_starts_from_last_evaluated_key(ddb, monkeypatch):
    seen = {}

    def second(_self, kw):
        seen["start"] = kw.get("ExclusiveStartKey")
        return {"Items": []}

    _with_dtc_table(monkeypatch, _PagedStub(second))
    resp = h.handler(_event(), None)
    assert resp["statusCode"] == 200
    assert seen["start"] == {"vehicleId": VEHICLE, "timestamp": 1}


def test_more_pages_than_the_cap_fails_closed(ddb, monkeypatch):
    # Keeps returning a next page. Past the cap it would stop and return a fault, so a
    # handler without the cap answers 200 instead of failing closed.
    def endless(self, _kw):
        if self.calls > h._DTC_MAX_PAGES + 5:
            return {"Items": [{"vehicleId": VEHICLE, "timestamp": 0, "code": "P0002",
                               "status": "ACTIVE"}]}
        return {"Items": [], "LastEvaluatedKey": {"vehicleId": VEHICLE, "timestamp": self.calls}}

    stub = _PagedStub(endless)
    _with_dtc_table(monkeypatch, stub)
    _assert_failure_response(h.handler(_event(), None))
    assert stub.calls == h._DTC_MAX_PAGES


def test_remote_clears_and_cleared_at_rows_are_not_active(ddb):
    """Issue 2026-10-05-active-dtcs-reads-remote-clears-as-active: the iOS health card used
    the same rule and kept remotely cleared faults."""
    t = _create_dtc_table(ddb)
    now = int(time.time() * 1000)
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now, "code": "P0300", "severity": "HIGH",
                     "status": "CLEARED_REMOTE", "clearedDate": "", "clearedAt": now - 5, "dtcId": "r1"})
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now - 1000, "code": "P0171", "severity": "HIGH",
                     "status": "ACTIVE", "clearedAt": now - 5, "dtcId": "r2"})
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now - 2000, "code": "P0299", "severity": "HIGH",
                     "status": "ACTIVE", "resetAt": "2026-10-01", "dtcId": "r3"})
    t.put_item(Item={"vehicleId": VEHICLE, "timestamp": now - 3000, "code": "P0420", "severity": "HIGH",
                     "status": "ACTIVE", "dtcId": "r4"})

    resp = h.handler(_event(), None)

    assert resp["statusCode"] == 200
    assert [d["code"] for d in _body(resp)["activeDtcs"]] == ["P0420"]
