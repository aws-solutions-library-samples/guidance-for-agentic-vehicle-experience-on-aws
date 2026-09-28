"""Tests for GET /findings/me and the owner-group reads on /findings/vehicle/{vin}
(spec 2026-09-25-avx-own-vehicle-findings, T1.2 / T2.1 / T2.2, review Cycle 1 fixes).

The route derives every standing from trusted claims only and returns the union of the
caller's Findings. The client sends no identifier. Standing names follow
docs/identity-and-standing-contract.md: owner, fleet_manager, fleet_driver.

Written red (strict xfail) in T1.2; T2.1 implemented the route and removed the marker.

Seams (patched below): the GSI helpers h._list_findings_by_vehicle (by_vehicle GSI),
_by_owner and _by_fleet, each returning (items, next_token). There is no CMS read: the
contract projects standing facts at write time (§ 6).
"""
from __future__ import annotations

import base64
import json
import os
import sys
from unittest.mock import patch

import pytest

_LAMBDA_ROOT = os.path.join(os.path.dirname(__file__), "..")
_LAMBDAS_ROOT = os.path.dirname(_LAMBDA_ROOT)
_REPO_ROOT = os.path.abspath(os.path.join(_LAMBDA_ROOT, "..", ".."))
for _p in (_LAMBDA_ROOT, _LAMBDAS_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import handler as h  # noqa: E402


_FUTURE = "2099-01-01T00:00:00Z"
_PAST = "2000-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _trust_all(monkeypatch):
    for env in ("AVX_CUSTOMER_ID_CLAIM_TRUSTED", "AVX_VEHICLE_ID_CLAIM_TRUSTED",
                "AVX_FLEET_IDS_CLAIM_TRUSTED"):
        monkeypatch.setenv(env, "true")


def _claims(*, groups=("vehicle-owner",), vehicle_id=None, customer_id=None, fleet_ids=None,
            fleet_ids_raw=None):
    """``fleet_ids`` is written as a JSON list; ``fleet_ids_raw`` verbatim (the live CSV form)."""
    c = {"sub": "sub-caller", "cognito:groups": json.dumps(list(groups))}
    if vehicle_id is not None:
        c["custom:vehicleId"] = vehicle_id
    if customer_id is not None:
        c["custom:customerId"] = customer_id
    if fleet_ids is not None:
        c["custom:fleetIds"] = json.dumps(fleet_ids)
    if fleet_ids_raw is not None:
        c["custom:fleetIds"] = fleet_ids_raw
    return c


def _event(claims, qs=None):
    return {"httpMethod": "GET", "resource": "/findings/me", "pathParameters": {},
            "queryStringParameters": qs or {},
            "requestContext": {"authorizer": {"claims": claims}}, "body": None}


def _f(fid, *, vin="VIN-A", vehicle_id="VEH-A", owner_id=None, fleet_id=None,
       expires_at=_FUTURE):
    return {"finding_id": fid, "vin": vin, "vehicle_id": vehicle_id, "owner_id": owner_id,
            "fleet_id": fleet_id, "expires_at": expires_at, "headline": fid}


def _call(claims, *, by_vehicle=(), by_owner=(), by_fleet=(), qs=None):
    """Invoke the route with the three GSI helpers stubbed."""
    with patch.object(h, "_list_findings_by_vehicle", return_value=(list(by_vehicle), None)) as m_veh, \
         patch.object(h, "_list_findings_by_owner", return_value=(list(by_owner), None)) as m_owner, \
         patch.object(h, "_list_findings_by_fleet", return_value=(list(by_fleet), None)) as m_fleet:
        resp = h.handler(_event(claims, qs), None)
    body = json.loads(resp["body"])
    return resp["statusCode"], body, {"vehicle": m_veh, "owner": m_owner, "fleet": m_fleet}


def _ids(body):
    return sorted(f["finding_id"] for f in body["findings"])


def _empty(body):
    return body["findings"] == [] and body["standings"] == [] and body["admitted_by"] == {}


def _eq_parts(cond):
    """(Key|Attr, attribute name, value) of a boto3 ``eq`` condition.

    Compared by parts rather than ``==``: boto3 overloads ``==`` on attributes to BUILD a
    condition, so comparing two conditions with ``==`` can be truthy for different names.
    """
    expr = cond.get_expression()
    assert expr["operator"] == "=", f"expected an equality condition, got {expr['operator']}"
    attr, value = expr["values"]
    return type(attr).__name__, attr.name, value


class _CaptureTable:
    def __init__(self):
        self.kwargs = {}

    def query(self, **kw):
        self.kwargs = kw
        return {"Items": []}


def _capture(fn, *args):
    table = _CaptureTable()
    with patch.object(h, "_get_ddb") as ddb:
        ddb.return_value.Table.return_value = table
        fn(*args)
    return table.kwargs


# ── driver standing ─────────────────────────────────────────────────────────

def test_driver_sees_only_findings_projected_to_their_vehicle():
    status, body, _ = _call(_claims(vehicle_id="VEH-A"),
                            by_vehicle=[_f("F-mine"), _f("F-other", vehicle_id="VEH-B")])
    assert status == 200
    assert _ids(body) == ["F-mine"]
    assert body["standings"] == ["fleet_driver"]
    assert body["admitted_by"] == {"F-mine": ["fleet_driver"]}


def test_untrusted_vehicle_claim_adds_nothing(monkeypatch):
    monkeypatch.setenv("AVX_VEHICLE_ID_CLAIM_TRUSTED", "false")
    status, body, m = _call(_claims(vehicle_id="VEH-A"), by_vehicle=[_f("F-mine")])
    assert status == 200 and _empty(body)
    m["vehicle"].assert_not_called()


def test_vehicle_claim_without_a_standing_group_adds_nothing():
    status, body, _ = _call(_claims(groups=(), vehicle_id="VEH-A"), by_vehicle=[_f("F-mine")])
    assert status == 200 and _empty(body)


def test_driver_with_no_findings_still_reports_the_standing():
    status, body, m = _call(_claims(vehicle_id="VEH-A"))
    assert status == 200 and body["findings"] == []
    assert body["standings"] == ["fleet_driver"]
    assert m["vehicle"].call_args.args[0] == "VEH-A"


# ── owner and fleet standing ───────────────────────────────────────────────

def test_owner_reads_by_trusted_customer_id():
    status, body, m = _call(_claims(customer_id="CUST-1"),
                            by_owner=[_f("F-owned", owner_id="CUST-1")])
    assert status == 200 and _ids(body) == ["F-owned"]
    assert body["standings"] == ["owner"]
    assert m["owner"].call_args.args[0] == "CUST-1"


def test_untrusted_customer_id_adds_nothing(monkeypatch):
    monkeypatch.setenv("AVX_CUSTOMER_ID_CLAIM_TRUSTED", "false")
    status, body, m = _call(_claims(customer_id="CUST-1"),
                            by_owner=[_f("F-owned", owner_id="CUST-1")])
    assert status == 200 and _empty(body)
    m["owner"].assert_not_called()


def test_fleet_manager_reads_each_trusted_fleet():
    status, body, m = _call(_claims(groups=("fleet-operator",), fleet_ids=["FLT-1"]),
                            by_fleet=[_f("F-fleet", fleet_id="FLT-1")])
    assert status == 200 and _ids(body) == ["F-fleet"]
    assert body["standings"] == ["fleet_manager"]
    assert m["fleet"].call_args.args[0] == "FLT-1"


def test_fleet_manager_reads_every_fleet_in_a_csv_claim():
    """Review Cycle 1 W2: the live claim is CSV ('flt-a,flt-b'), not a JSON list."""
    status, body, m = _call(
        _claims(groups=("fleet-operator",), fleet_ids_raw="flt-meridian-range-001,FLEET-DEMO-PUBLIC"),
        by_fleet=[_f("F-fleet", fleet_id="FLEET-DEMO-PUBLIC")])
    assert status == 200 and body["standings"] == ["fleet_manager"]
    assert sorted(c.args[0] for c in m["fleet"].call_args_list) == [
        "FLEET-DEMO-PUBLIC", "flt-meridian-range-001"]
    assert _ids(body) == ["F-fleet"]


def test_untrusted_fleet_claim_adds_nothing(monkeypatch):
    """Review Cycle 1 W2: AVX_FLEET_IDS_CLAIM_TRUSTED=false means no fleet read at all."""
    monkeypatch.setenv("AVX_FLEET_IDS_CLAIM_TRUSTED", "false")
    status, body, m = _call(_claims(groups=("fleet-operator",), fleet_ids=["FLT-1"]),
                            by_fleet=[_f("F-fleet", fleet_id="FLT-1")])
    assert status == 200 and _empty(body)
    m["fleet"].assert_not_called()


@pytest.mark.parametrize("group", ["fleet-driver", "vehicle-owner", "consumer"])
def test_fleet_standing_needs_the_fleet_operator_group(group):
    """Review Cycle 1 W3: as on /findings/fleet/{id} (check_fleet_scope), only
    fleet-operator gets fleet-wide reads. A fleet driver with custom:fleetIds sees only
    the vehicle they drive."""
    status, body, m = _call(_claims(groups=(group,), vehicle_id="VEH-A", fleet_ids=["FLT-1"]),
                            by_vehicle=[_f("F-mine")],
                            by_fleet=[_f("F-fleet", vehicle_id="VEH-B", fleet_id="FLT-1")])
    assert status == 200
    m["fleet"].assert_not_called()
    assert body["standings"] == ["fleet_driver"]
    assert _ids(body) == ["F-mine"]


# ── union, expiry, identifiers, paging ──────────────────────────────────────

def test_union_is_deduplicated_and_tagged_with_every_standing():
    same = _f("F-both", owner_id="CUST-1")
    status, body, _ = _call(_claims(vehicle_id="VEH-A", customer_id="CUST-1"),
                            by_vehicle=[same], by_owner=[same])
    assert status == 200 and _ids(body) == ["F-both"]
    assert body["admitted_by"] == {"F-both": ["fleet_driver", "owner"]}
    assert body["standings"] == ["fleet_driver", "owner"]


def test_no_standing_is_an_empty_answer_not_an_error():
    status, body, m = _call(_claims(groups=()))
    assert status == 200 and _empty(body)
    for mock in m.values():
        mock.assert_not_called()


def test_expired_findings_are_hidden():
    status, body, _ = _call(_claims(vehicle_id="VEH-A"),
                            by_vehicle=[_f("F-live"), _f("F-expired", expires_at=_PAST)])
    assert status == 200 and _ids(body) == ["F-live"]


def test_query_string_identifiers_are_ignored():
    status, body, m = _call(_claims(vehicle_id="VEH-A"), by_vehicle=[_f("F-mine")],
                            qs={"owner_id": "CUST-OTHER", "vin": "VIN-OTHER",
                                "vehicle_id": "VEH-B"})
    assert status == 200 and _ids(body) == ["F-mine"]
    m["owner"].assert_not_called()
    assert [c.args[0] for c in m["vehicle"].call_args_list] == ["VEH-A"]


def test_bad_page_size_is_ignored():
    """Review Cycle 1 S7: /findings/me takes no paging parameters, so a malformed
    page_size must not turn into an unhandled error (a 502 at API Gateway)."""
    status, body, _ = _call(_claims(vehicle_id="VEH-A"), by_vehicle=[_f("F-mine")],
                            qs={"page_size": "not-a-number"})
    assert status == 200 and _ids(body) == ["F-mine"]


def test_every_gsi_page_is_read_and_the_cursor_is_passed_forward():
    """T2.1 Accept 'paged to exhaustion'; review Cycle 1 W5: the second call must carry
    the first call's cursor, or DynamoDB returns page 1 again."""
    pages = [([_f("F-p1")], "tok-1"), ([_f("F-p2")], None)]
    with patch.object(h, "_list_findings_by_vehicle", side_effect=pages) as m_veh, \
         patch.object(h, "_list_findings_by_owner", return_value=([], None)), \
         patch.object(h, "_list_findings_by_fleet", return_value=([], None)):
        resp = h.handler(_event(_claims(vehicle_id="VEH-A")), None)
    body = json.loads(resp["body"])
    assert _ids(body) == ["F-p1", "F-p2"]
    assert [c.kwargs.get("next_token") for c in m_veh.call_args_list] == [None, "tok-1"]
    assert body["truncated"] is False


def test_page_cap_is_reported_as_truncated():
    """Review Cycle 1 S1: at _ME_MAX_PAGES the read stops and the response says so."""
    counter = iter(range(10_000))

    def endless(key, limit=200, next_token=None):
        n = next(counter)
        return [_f(f"F-{n:05d}")], f"tok-{n}"

    with patch.object(h, "_list_findings_by_vehicle", side_effect=endless) as m_veh, \
         patch.object(h, "_list_findings_by_owner", return_value=([], None)), \
         patch.object(h, "_list_findings_by_fleet", return_value=([], None)):
        resp = h.handler(_event(_claims(vehicle_id="VEH-A")), None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert m_veh.call_count == h._ME_MAX_PAGES
    assert len(body["findings"]) == h._ME_MAX_PAGES
    assert body["truncated"] is True


def test_a_failed_read_is_a_500_not_a_partial_answer():
    """Review Cycle 1 W4: an unhandled exception (a missing GSI before it is ACTIVE)
    became a Lambda error and an API Gateway 502 for the whole response."""
    with patch.object(h, "_list_findings_by_vehicle", side_effect=RuntimeError("no index")), \
         patch.object(h, "_list_findings_by_owner", return_value=([_f("F-owned", owner_id="CUST-1")], None)), \
         patch.object(h, "_list_findings_by_fleet", return_value=([], None)):
        resp = h.handler(_event(_claims(vehicle_id="VEH-A", customer_id="CUST-1")), None)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"]) == {"error": "internal error"}


def test_findings_are_returned_contract_shaped():
    status, body, _ = _call(_claims(vehicle_id="VEH-A", customer_id="CUST-1"),
                            by_vehicle=[_f("F-mine", owner_id="CUST-1", fleet_id="FLT-1")])
    assert status == 200
    leaked = h._PROJECTED_SCOPE_ATTRS & set(body["findings"][0])
    assert not leaked, f"projected scope attributes reached the response: {leaked}"
    assert "admitted_by" not in body["findings"][0]


def test_by_vehicle_helper_queries_the_by_vehicle_index():
    """Review Cycle 1 S2: by_vin/vin is also a valid pair, so only a pin catches a swap."""
    kw = _capture(h._list_findings_by_vehicle, "VEH-A")
    assert kw["IndexName"] == "by_vehicle"
    assert _eq_parts(kw["KeyConditionExpression"]) == ("Key", "vehicle_id", "VEH-A")


# ── T2.2: GET /findings/vehicle/{vin} for owner-group callers ────────────────

def _vehicle_event(claims, vin="VIN-A", qs=None):
    return {"httpMethod": "GET", "resource": "/findings/vehicle/{vin}",
            "pathParameters": {"vin": vin}, "queryStringParameters": qs or {},
            "requestContext": {"authorizer": {"claims": claims}}, "body": None}


def _vehicle_route(claims, vin="VIN-A", *, owner_rows=None, vehicle_rows=None):
    if owner_rows is None:
        owner_rows = [_f("F-owned", owner_id="CUST-1")]
    if vehicle_rows is None:
        vehicle_rows = [_f("F-driven")]
    unfiltered = patch.object(h, "_list_findings_by_vin",
                              side_effect=AssertionError("owner caller got an unfiltered read"))
    with unfiltered, \
         patch.object(h, "_list_owner_findings_for_vin",
                      return_value=(owner_rows, None)) as m_owner, \
         patch.object(h, "_list_vehicle_findings_for_vin",
                      return_value=(vehicle_rows, None)) as m_veh:
        resp = h.handler(_vehicle_event(claims, vin), None)
    return resp["statusCode"], json.loads(resp["body"]), m_owner, m_veh


def test_vehicle_route_owner_filters_by_trusted_customer_id():
    status, body, m_owner, m_veh = _vehicle_route(_claims(customer_id="CUST-1"))
    assert status == 200 and _ids(body) == ["F-owned"]
    assert m_owner.call_args.args[:2] == ("CUST-1", "VIN-A")
    m_veh.assert_not_called()


def test_vehicle_route_driver_filters_by_trusted_vehicle_id():
    status, body, m_owner, m_veh = _vehicle_route(_claims(vehicle_id="VEH-A"))
    assert status == 200 and _ids(body) == ["F-driven"]
    assert m_veh.call_args.args[:2] == ("VEH-A", "VIN-A")
    m_owner.assert_not_called()


def test_vehicle_route_without_a_trusted_identity_keeps_the_503(monkeypatch):
    monkeypatch.setenv("AVX_CUSTOMER_ID_CLAIM_TRUSTED", "false")
    monkeypatch.setenv("AVX_VEHICLE_ID_CLAIM_TRUSTED", "false")
    status, body, m_owner, m_veh = _vehicle_route(_claims(customer_id="CUST-1", vehicle_id="VEH-A"))
    assert status == 503 and body["error"] == "owner_scope_unavailable"
    m_owner.assert_not_called()
    m_veh.assert_not_called()


def test_vehicle_route_response_is_contract_shaped():
    _, body, _, _ = _vehicle_route(_claims(vehicle_id="VEH-A"))
    assert not (h._PROJECTED_SCOPE_ATTRS & set(body["findings"][0]))


@pytest.mark.parametrize("claims,rows_key", [
    (dict(customer_id="CUST-1"), "owner_rows"),
    (dict(vehicle_id="VEH-A"), "vehicle_rows"),
])
def test_vehicle_route_keeps_only_live_rows_matching_the_vin_and_the_caller(claims, rows_key):
    """Defence in depth plus review Cycle 1 S5: rows whose vin or projected identity
    disagree with the query are dropped, and expired Findings are hidden."""
    rows = [
        _f("F-good", owner_id="CUST-1"),
        _f("F-other-vin", vin="VIN-B", owner_id="CUST-1"),
        _f("F-other-party", vehicle_id="VEH-B", owner_id="CUST-2"),
        _f("F-expired", owner_id="CUST-1", expires_at=_PAST),
    ]
    status, body, _, _ = _vehicle_route(_claims(**claims), **{rows_key: rows})
    assert status == 200 and _ids(body) == ["F-good"]


@pytest.mark.parametrize("claims,helper", [
    (dict(customer_id="CUST-1"), "_list_owner_findings_for_vin"),
    (dict(vehicle_id="VEH-A"), "_list_vehicle_findings_for_vin"),
])
def test_vehicle_route_failed_read_is_a_500_not_an_empty_page(claims, helper):
    """Review Cycle 2 S10: a failed read (a missing GSI before it is ACTIVE) must not
    read as 'this vehicle has no Findings'."""
    # A recording mock, not side_effect=AssertionError: the route's except clause would
    # turn an AssertionError into the very 500 this test expects (review Cycle 3 S19).
    with patch.object(h, "_list_findings_by_vin", return_value=([], None)) as unfiltered, \
         patch.object(h, helper, side_effect=RuntimeError("ValidationException: no index")) as m:
        resp = h.handler(_vehicle_event(_claims(**claims)), None)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"]) == {"error": "internal error"}
    m.assert_called_once()
    unfiltered.assert_not_called()


@pytest.mark.parametrize("fn,index,key,caller", [
    (h._list_owner_findings_for_vin, "by_owner", "owner_id", "CUST-1"),
    (h._list_vehicle_findings_for_vin, "by_vehicle", "vehicle_id", "VEH-A"),
])
def test_vehicle_route_queries_the_callers_own_partition(fn, index, key, caller):
    """Review Cycle 1 C1/W1 pin: the key is the caller's own identity and the requested
    vin is only a filter. Swapping them (the VIN's partition, filtered on the caller)
    re-opens the next_token oracle; dropping the filter returns the caller's other
    vehicles' Findings under this VIN's URL."""
    kw = _capture(fn, caller, "VIN-A", 7)
    assert kw["IndexName"] == index
    assert _eq_parts(kw["KeyConditionExpression"]) == ("Key", key, caller)
    assert _eq_parts(kw["FilterExpression"]) == ("Attr", "vin", "VIN-A")
    assert kw["Limit"] == 7


# ── W1: page_size must not turn next_token into an existence oracle ──────────

class _FakeFindingsTable:
    """In-memory Query with DynamoDB's order of operations: evaluate up to ``Limit``
    items of the key's partition, THEN apply the filter, and return LastEvaluatedKey
    whenever ``Limit`` items were evaluated."""

    def __init__(self, items):
        self.items = sorted(items, key=lambda i: i["finding_id"])

    def query(self, **kw):
        _, key_name, key_value = _eq_parts(kw["KeyConditionExpression"])
        part = [i for i in self.items if i.get(key_name) == key_value]
        esk = kw.get("ExclusiveStartKey")
        if esk:
            if esk.get(key_name) != key_value:
                # DynamoDB rejects a start key outside the queried partition.
                raise RuntimeError("ValidationException: The provided starting key is invalid")
            ids = [i["finding_id"] for i in part]
            part = part[ids.index(esk["finding_id"]) + 1:]
        evaluated = part[:kw["Limit"]]
        kept = evaluated
        if "FilterExpression" in kw:
            _, f_name, f_value = _eq_parts(kw["FilterExpression"])
            kept = [i for i in evaluated if i.get(f_name) == f_value]
        resp = {"Items": kept}
        if len(evaluated) == kw["Limit"]:
            last = evaluated[-1]
            resp["LastEvaluatedKey"] = {"finding_id": last["finding_id"], key_name: key_value}
        return resp


def _mine():
    return [_f("F-mine-1", owner_id="CUST-1"), _f("F-mine-2", owner_id="CUST-1")]


def _foreign():
    return _f("F-foreign", vin="VIN-X", vehicle_id="VEH-X", owner_id="CUST-2")


def _vehicle_route_on(table_items, claims, vin, page_size):
    table = _FakeFindingsTable(table_items)
    with patch.object(h, "_get_ddb") as ddb, \
         patch.object(h, "_list_findings_by_vin",
                      side_effect=AssertionError("owner caller got an unfiltered read")):
        ddb.return_value.Table.return_value = table
        resp = h.handler(_vehicle_event(claims, vin, {"page_size": str(page_size)}), None)
    body = json.loads(resp["body"])
    body.pop("computed_at", None)
    return resp["statusCode"], body


@pytest.mark.parametrize("claims", [dict(customer_id="CUST-1"), dict(vehicle_id="VEH-A")])
def test_foreign_vin_response_does_not_depend_on_other_parties_findings(claims):
    """Review Cycle 1 W1: with page_size=1, the answer for a VIN the caller has no
    standing over is the same whether or not someone else has Findings on it, and any
    cursor names only the caller's own rows."""
    with_foreign = _vehicle_route_on(_mine() + [_foreign()], _claims(**claims), "VIN-X", 1)
    without = _vehicle_route_on(_mine(), _claims(**claims), "VIN-X", 1)
    assert with_foreign == without
    status, body = with_foreign
    assert status == 200 and body["findings"] == []
    token = body["next_token"]
    if token:
        start = json.loads(base64.b64decode(token))
        assert start["finding_id"] in {f["finding_id"] for f in _mine()}


def test_fake_table_models_the_oracle_it_guards():
    """Control: the VIN-partition query the route used before Cycle 1 DOES answer
    differently for the two table states, so the test above is not vacuous."""
    def page(items):
        with patch.object(h, "_get_ddb") as ddb:
            ddb.return_value.Table.return_value = _FakeFindingsTable(items)
            return h._list_findings_by_vin_for_owner("VIN-X", "CUST-1", 1)

    assert page(_mine() + [_foreign()])[1] is not None
    assert page(_mine())[1] is None


@pytest.mark.parametrize("claims,key,foreign_value", [
    (dict(customer_id="CUST-1"), "owner_id", "CUST-2"),
    (dict(vehicle_id="VEH-A"), "vehicle_id", "VEH-X"),
])
def test_forged_cursor_cannot_move_the_query_off_the_callers_partition(claims, key, foreign_value):
    """Security review Cycle 1 S2: a hand-made next_token naming another party's row
    neither changes the key condition nor returns that row. The fake raises as
    DynamoDB does for a start key outside the queried partition, and the route turns
    that into its generic 500."""
    forged = base64.b64encode(json.dumps(
        {"finding_id": "F-foreign", key: foreign_value}).encode()).decode()
    table = _FakeFindingsTable(_mine() + [_foreign()])
    calls = []
    real_query = table.query

    def recording_query(**kw):
        calls.append(kw)
        return real_query(**kw)

    table.query = recording_query
    with patch.object(h, "_get_ddb") as ddb, \
         patch.object(h, "_list_findings_by_vin", return_value=([], None)) as unfiltered:
        ddb.return_value.Table.return_value = table
        resp = h.handler(_vehicle_event(_claims(**claims), "VIN-X",
                                        {"page_size": "1", "next_token": forged}), None)
    unfiltered.assert_not_called()
    assert len(calls) == 1
    caller_value = claims.get("customer_id") or claims.get("vehicle_id")
    assert _eq_parts(calls[0]["KeyConditionExpression"])[1:] == (key, caller_value)
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"]) == {"error": "internal error"}


def test_routes_never_call_the_vin_partition_helpers():
    """Security review Cycle 1 S3: the two retired helpers keep the W1 shape (VIN
    partition, caller filter). They exist only for shape pins; no route may call them."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(h))
    retired = {"_list_findings_by_vin_for_owner", "_list_findings_by_vin_for_fleet"}
    called = sorted(
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in retired)
    assert called == [], f"a route calls a retired VIN-partition helper: {called}"
    for name in retired:
        assert hasattr(h, name), f"{name} was removed; drop it from this guard"


def test_owner_can_page_through_their_own_findings_on_a_vin():
    """The cursor the route returns resumes inside the caller's own partition."""
    rows = [_f(f"F-{n}", owner_id="CUST-1") for n in range(3)] + [_foreign()]
    table = _FakeFindingsTable(rows)
    seen, token = [], None
    with patch.object(h, "_get_ddb") as ddb, \
         patch.object(h, "_list_findings_by_vin", side_effect=AssertionError("unfiltered")):
        ddb.return_value.Table.return_value = table
        for _ in range(5):
            qs = {"page_size": "1", **({"next_token": token} if token else {})}
            resp = h.handler(_vehicle_event(_claims(customer_id="CUST-1"), "VIN-A", qs), None)
            body = json.loads(resp["body"])
            seen += [f["finding_id"] for f in body["findings"]]
            token = body["next_token"]
            if not token:
                break
    assert seen == ["F-0", "F-1", "F-2"]


# ── defence in depth: a GSI row whose projected fact disagrees is dropped ────

def test_owner_rows_with_a_different_owner_id_are_dropped():
    status, body, _ = _call(_claims(customer_id="CUST-1"),
                            by_owner=[_f("F-owned", owner_id="CUST-1"),
                                      _f("F-stray", owner_id="CUST-2")])
    assert status == 200 and _ids(body) == ["F-owned"]


def test_fleet_rows_with_a_different_fleet_id_are_dropped():
    status, body, _ = _call(_claims(groups=("fleet-operator",), fleet_ids=["FLT-1"]),
                            by_fleet=[_f("F-fleet", fleet_id="FLT-1"),
                                      _f("F-stray", fleet_id="FLT-2")])
    assert status == 200 and _ids(body) == ["F-fleet"]
