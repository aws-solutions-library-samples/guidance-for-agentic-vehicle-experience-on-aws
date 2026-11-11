"""owner_id is the vehicle's assigned driver, never its purchaser.

Issue ``issues/2026-10-07-avx-finding-owner-is-purchaser-not-driver/``.  The projected
``owner_id`` decides who may read a Finding by owner, who the broker cards, and who the
chat executor accepts, so every property below is an authorization property:

* the owner is the CMS-pool ``sub`` of the vehicle's ONE assigned driver;
* ``sold_to`` (the purchaser) is never read and never projected;
* an ambiguous assignment, an ambiguous or unverified pool user, a disabled user, a
  malformed address or sub, and every client error project NO owner;
* the drivers table is read once per run, paged to completion, and so is ListUsers.

Fakes return what the code asked for: the DynamoDB fake answers by ``vehicleId`` and
the Cognito fake by the email inside the filter, so a resolver that asked for the wrong
thing gets the wrong answer.
"""
from __future__ import annotations

import ast
import pathlib
import re
from typing import Any, Optional

import pytest

from agents.tier2 import owner_resolver as OR
from agents.tier2.owner_resolver import AssignedDriverDirectory, StandingFacts

VIN = "MRDN0000000000015"
VEHICLE = "VEH-TEST-0001"  # test-only id (vehicleId != vin; spec 2026-10-28 T2.5)
FLEET = "FLEET-MERIDIAN-ONBOARD"
PURCHASER = "CUST-004000DD"
EMAIL = "meridian.driver@example.com"
DRIVER_SUB = "1111aaaa-2222-4333-8444-5555bbbb5555"
OTHER_SUB = "aaaaaaaa-0000-4000-8000-000000000001"
POOL = "us-west-2_TestP1"  # under 8 chars: the publish scanner flags pool-id shapes

_REPO = pathlib.Path(__file__).resolve().parents[3]


def _driver(driver_id: str, vehicle: Optional[str], email: Optional[str]) -> dict[str, Any]:
    item: dict[str, Any] = {"driverId": {"S": driver_id}}
    if vehicle is not None:
        item["assignedVehicleId"] = {"S": vehicle}
    if email is not None:
        item["cognitoEmail"] = {"S": email}
    return item


def _user(sub: str, email: str, *, verified: Optional[str] = "true", enabled: Any = True,
          status: str = "CONFIRMED") -> dict[str, Any]:
    attrs = [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": email}]
    if verified is not None:
        attrs.append({"Name": "email_verified", "Value": verified})
    return {"Username": sub, "Attributes": attrs, "Enabled": enabled, "UserStatus": status}


class _Registry:
    """The CMS vehicle registry: vin-index Query + GetItem, keyed on what was asked."""

    def __init__(self, rows: dict[str, dict[str, Any]], vin_to_vehicle: dict[str, str]):
        self.rows = rows
        self.vin_to_vehicle = vin_to_vehicle
        self.get_item_calls: list[dict[str, Any]] = []

    def query(self, **kw: Any) -> dict[str, Any]:
        vin = kw["ExpressionAttributeValues"][":vin"]["S"]
        vid = self.vin_to_vehicle.get(vin)
        return {"Items": [{"vehicleId": {"S": vid}, "vin": {"S": vin}}] if vid else []}

    def get_item(self, **kw: Any) -> dict[str, Any]:
        self.get_item_calls.append(kw)
        row = self.rows.get(kw["Key"]["vehicleId"]["S"], {})
        names = set((kw.get("ExpressionAttributeNames") or {}).values())
        # Return only what was projected, as DynamoDB does.
        return {"Item": {k: v for k, v in row.items() if not names or k in names}}


class _DriversTable:
    """cms-{stage}-storage-drivers Scan, split into pages of ``page_size``."""

    def __init__(self, items: list[dict[str, Any]], page_size: int = 1000, fail: bool = False):
        self.items = items
        self.page_size = page_size
        self.fail = fail
        self.scan_calls: list[dict[str, Any]] = []

    def scan(self, **kw: Any) -> dict[str, Any]:
        self.scan_calls.append(kw)
        if self.fail:
            raise RuntimeError("AccessDeniedException")
        start = int((kw.get("ExclusiveStartKey") or {}).get("i", {}).get("N", "0"))
        page = self.items[start:start + self.page_size]
        out: dict[str, Any] = {"Items": page}
        if start + self.page_size < len(self.items):
            out["LastEvaluatedKey"] = {"i": {"N": str(start + self.page_size)}}
        return out


class _Pool:
    """Cognito ListUsers: answers from the email inside ``Filter``, in pages."""

    def __init__(self, users: list[dict[str, Any]], page_size: int = 60, fail: bool = False):
        self.users = users
        self.page_size = page_size
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def list_users(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("TooManyRequestsException")
        m = re.fullmatch(r'email = "([^"]*)"', kw["Filter"])
        assert m, f"unexpected filter {kw['Filter']!r}"
        want = m.group(1).lower()
        hits = [u for u in self.users
                if any(a["Name"] == "email" and a["Value"].lower() == want for a in u["Attributes"])]
        start = int(kw.get("PaginationToken") or 0)
        out: dict[str, Any] = {"Users": hits[start:start + self.page_size]}
        if start + self.page_size < len(hits):
            out["PaginationToken"] = str(start + self.page_size)
        return out


def _directory(drivers: list[dict[str, Any]], users: list[dict[str, Any]], **kw: Any):
    table = kw.pop("table", None) or _DriversTable(drivers)
    pool = kw.pop("pool", None) or _Pool(users)
    d = AssignedDriverDirectory(ddb_client=table, drivers_table="cms-test-storage-drivers",
                                cognito_client=pool, user_pool_id=kw.pop("pool_id", POOL))
    return d, table, pool


def _registry() -> _Registry:
    return _Registry(
        rows={VEHICLE: {"vehicleId": {"S": VEHICLE}, "fleetId": {"S": FLEET},
                        "sold_to": {"S": PURCHASER}}},
        vin_to_vehicle={VIN: VEHICLE},
    )


def _resolve(directory: Optional[AssignedDriverDirectory], registry: Optional[_Registry] = None):
    return OR.resolve_standing_facts(VIN, ddb_client=registry or _registry(),
                                     vehicles_table="cms-test-storage-vehicles", drivers=directory)



# ---------------------------------------------------------------------------
# The positive path, and the purchaser is never the owner
# ---------------------------------------------------------------------------


def test_owner_is_the_assigned_drivers_sub_not_the_purchaser():
    """The vehicle row carries sold_to; owner_id must be the driver's sub regardless."""
    d, table, pool = _directory([_driver("DRV-MRDN-0015", VEHICLE, EMAIL)],
                                [_user(DRIVER_SUB, EMAIL)])
    facts = _resolve(d)
    assert facts == StandingFacts(owner_id=DRIVER_SUB, fleet_id=FLEET, vehicle_id=VEHICLE)
    assert facts.owner_id != PURCHASER
    assert pool.calls[0]["UserPoolId"] == POOL
    assert pool.calls[0]["Filter"] == f'email = "{EMAIL}"'


def test_sold_to_is_not_read_from_the_registry():
    """The GetItem projects fleetId only: the purchaser is not even fetched."""
    registry = _registry()
    d, _, _ = _directory([_driver("DRV-MRDN-0015", VEHICLE, EMAIL)], [_user(DRIVER_SUB, EMAIL)])
    _resolve(d, registry)
    (call,) = registry.get_item_calls
    assert set(call["ExpressionAttributeNames"].values()) == {"fleetId"}
    assert "sold_to" not in repr(call)


def test_purchaser_is_not_projected_when_the_vehicle_has_no_driver():
    """No driver: no owner. sold_to must not become a fallback."""
    d, _, pool = _directory([], [])
    facts = _resolve(d)
    assert facts == StandingFacts(owner_id=None, fleet_id=FLEET, vehicle_id=VEHICLE)
    assert pool.calls == []


def test_no_directory_means_no_owner_and_other_facts_still_resolve():
    assert _resolve(None) == StandingFacts(owner_id=None, fleet_id=FLEET, vehicle_id=VEHICLE)


def test_drivers_is_a_required_keyword():
    """Omitting the directory must be a TypeError, so no call site drops it silently."""
    with pytest.raises(TypeError):
        OR.resolve_standing_facts(VIN, ddb_client=_registry(), vehicles_table="t")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        OR.resolve_owner_id(VIN, ddb_client=_registry(), vehicles_table="t")  # type: ignore[call-arg]


def test_resolve_owner_id_is_the_owner_view():
    d, _, _ = _directory([_driver("DRV-1", VEHICLE, EMAIL)], [_user(DRIVER_SUB, EMAIL)])
    assert OR.resolve_owner_id(VIN, ddb_client=_registry(), vehicles_table="t", drivers=d) == DRIVER_SUB


# ---------------------------------------------------------------------------
# The assignment must be unambiguous
# ---------------------------------------------------------------------------


def test_two_drivers_on_one_vehicle_project_no_owner():
    """MRDN0000002000013 on staging has three. Picking one would hand the vehicle to a guess."""
    d, _, pool = _directory(
        [_driver("DRV-A", VEHICLE, EMAIL), _driver("DRV-B", VEHICLE, "other@example.com")],
        [_user(DRIVER_SUB, EMAIL), _user(OTHER_SUB, "other@example.com")],
    )
    assert _resolve(d).owner_id is None
    assert d.lookup(VEHICLE) == (None, "multiple_assigned_drivers")
    assert pool.calls == []


def test_a_second_driver_without_an_email_still_makes_it_ambiguous():
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL), _driver("DRV-B", VEHICLE, None)],
                         [_user(DRIVER_SUB, EMAIL)])
    assert _resolve(d).owner_id is None


def test_a_driver_with_no_cognito_email_projects_no_owner():
    d, _, pool = _directory([_driver("DRV-A", VEHICLE, None)], [])
    assert d.lookup(VEHICLE) == (None, "driver_has_no_cognito_email")
    assert pool.calls == []


def test_drivers_on_other_vehicles_do_not_count():
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL), _driver("DRV-B", "VEH-OTHER", EMAIL),
                          _driver("DRV-C", None, EMAIL)],
                         [_user(DRIVER_SUB, EMAIL)])
    assert _resolve(d).owner_id == DRIVER_SUB


def test_the_driver_scan_is_paged_to_completion():
    """A second driver for the vehicle on page 2 makes it ambiguous; a reader that
    stopped at page 1 would project page 1's driver."""
    table = _DriversTable([_driver("DRV-A", VEHICLE, EMAIL)]
                          + [_driver(f"DRV-X{i}", f"VEH-{i}", None) for i in range(5)]
                          + [_driver("DRV-B", VEHICLE, "other@example.com")], page_size=2)
    d, _, _ = _directory([], [_user(DRIVER_SUB, EMAIL)], table=table)
    assert _resolve(d).owner_id is None
    assert len(table.scan_calls) == 4


def test_an_assignment_found_only_on_a_later_page_resolves():
    table = _DriversTable([_driver(f"DRV-X{i}", f"VEH-{i}", None) for i in range(5)]
                          + [_driver("DRV-A", VEHICLE, EMAIL)], page_size=2)
    d, _, _ = _directory([], [_user(DRIVER_SUB, EMAIL)], table=table)
    assert _resolve(d).owner_id == DRIVER_SUB


def test_the_scan_reads_only_the_three_join_attributes():
    d, table, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)], [_user(DRIVER_SUB, EMAIL)])
    _resolve(d)
    call = table.scan_calls[0]
    assert set(call["ExpressionAttributeNames"].values()) == {"driverId", "assignedVehicleId",
                                                              "cognitoEmail"}
    assert call["TableName"] == "cms-test-storage-drivers"



# ---------------------------------------------------------------------------
# The pool user must be one, verified, enabled, and sub-shaped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "users, reason",
    [
        ([], "no_pool_user"),
        ([_user(DRIVER_SUB, EMAIL), _user(OTHER_SUB, EMAIL, status="EXTERNAL_PROVIDER")],
         "multiple_pool_users"),
        ([_user(DRIVER_SUB, EMAIL, verified="false")], "pool_user_email_unverified"),
        ([_user(DRIVER_SUB, EMAIL, verified=None)], "pool_user_email_unverified"),
        ([_user(DRIVER_SUB, EMAIL, verified="True")], "pool_user_email_unverified"),
        ([_user(DRIVER_SUB, EMAIL, enabled=False)], "pool_user_disabled"),
        ([_user(DRIVER_SUB, EMAIL, enabled="true")], "pool_user_disabled"),
        ([_user("CUST-004000DD", EMAIL)], "pool_user_sub_malformed"),
        ([_user(DRIVER_SUB.upper(), EMAIL)], "pool_user_sub_malformed"),
    ],
    ids=["none", "two", "unverified", "verified-missing", "verified-not-lowercase-true",
         "disabled", "enabled-not-bool", "sub-is-a-party-id", "sub-uppercase"],
)
def test_pool_user_must_be_unique_verified_enabled_and_sub_shaped(users, reason):
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)], users)
    assert d.lookup(VEHICLE) == (None, reason)
    assert _resolve(d).owner_id is None


def test_an_unverified_claimant_does_not_take_the_drivers_vehicle():
    """The attack the email check exists for: another user set the driver's address
    without verifying it. The only match is unverified, so no owner."""
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)],
                         [_user(OTHER_SUB, EMAIL, verified="false")])
    assert _resolve(d).owner_id is None


def test_a_second_matching_user_on_a_later_page_makes_it_ambiguous():
    pool = _Pool([_user(DRIVER_SUB, EMAIL), _user(OTHER_SUB, EMAIL)], page_size=1)
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)], [], pool=pool)
    assert d.lookup(VEHICLE) == (None, "multiple_pool_users")
    assert len(pool.calls) == 2
    assert pool.calls[1]["PaginationToken"] == "1"


def test_email_match_is_case_insensitive_and_exact():
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, "Meridian.Driver@Example.com")],
                         [_user(DRIVER_SUB, EMAIL)])
    assert _resolve(d).owner_id == DRIVER_SUB


def test_a_returned_user_with_a_different_address_is_ignored():
    """If the filter ever returned a near-miss (prefix match), it must not count."""

    class _LoosePool(_Pool):
        def list_users(self, **kw: Any) -> dict[str, Any]:
            self.calls.append(kw)
            return {"Users": [_user(OTHER_SUB, "meridian.driver@example.com.evil")]}

    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)], [], pool=_LoosePool([]))
    assert d.lookup(VEHICLE) == (None, "no_pool_user")


@pytest.mark.parametrize("bad", ['x"@example.com', "a b@example.com", "a\\b@example.com",
                                 "no-at-sign", 'a@example.com" or email ^= "'])
def test_a_malformed_email_is_never_put_in_a_filter(bad):
    d, _, pool = _directory([_driver("DRV-A", VEHICLE, bad)], [_user(DRIVER_SUB, EMAIL)])
    assert d.lookup(VEHICLE) == (None, "driver_email_malformed")
    assert pool.calls == []


# ---------------------------------------------------------------------------
# Failures fail closed, and the run's answers are stable
# ---------------------------------------------------------------------------


def test_a_failed_driver_scan_projects_no_owner_and_is_not_retried(caplog):
    table = _DriversTable([_driver("DRV-A", VEHICLE, EMAIL)], fail=True)
    d, _, pool = _directory([], [_user(DRIVER_SUB, EMAIL)], table=table)
    facts = _resolve(d)
    assert facts == StandingFacts(owner_id=None, fleet_id=FLEET, vehicle_id=VEHICLE)
    assert d.lookup(VEHICLE) == (None, "driver_directory_unavailable")
    assert len(table.scan_calls) == 1, "a failed scan is remembered for the run"
    assert pool.calls == []
    assert "event=driver_directory_failed" in caplog.text


def test_a_failed_pool_lookup_projects_no_owner(caplog):
    d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)], [], pool=_Pool([], fail=True))
    assert _resolve(d).owner_id is None
    assert "event=pool_lookup_failed" in caplog.text


@pytest.mark.parametrize("pool_id", [None, "", "   "])
def test_an_unconfigured_pool_projects_no_owner_and_calls_nothing(pool_id):
    d, table, pool = _directory([_driver("DRV-A", VEHICLE, EMAIL)], [_user(DRIVER_SUB, EMAIL)],
                                pool_id=pool_id)
    assert d.lookup(VEHICLE) == (None, "driver_directory_unconfigured")
    assert table.scan_calls == [] and pool.calls == []


def test_one_scan_and_one_pool_query_per_run():
    d, table, pool = _directory([_driver("DRV-A", VEHICLE, EMAIL)], [_user(DRIVER_SUB, EMAIL)])
    first = [_resolve(d) for _ in range(3)]
    assert len({f for f in first}) == 1
    assert len(table.scan_calls) == 1
    assert len(pool.calls) == 1


def test_lookup_never_raises_on_a_broken_client():
    class _Broken:
        def scan(self, **kw: Any) -> dict[str, Any]:
            return None  # type: ignore[return-value]  # not a dict: .get raises

    d = AssignedDriverDirectory(ddb_client=_Broken(), drivers_table="t",
                                cognito_client=_Pool([]), user_pool_id=POOL)
    assert d.lookup(VEHICLE)[0] is None


def test_no_email_or_sub_is_logged(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    for users in ([], [_user(DRIVER_SUB, EMAIL, verified="false")], [_user(DRIVER_SUB, EMAIL)]):
        d, _, _ = _directory([_driver("DRV-A", VEHICLE, EMAIL)], users)
        _resolve(d)
    assert EMAIL not in caplog.text
    assert DRIVER_SUB not in caplog.text
    assert VIN not in caplog.text


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------


def test_every_runner_call_passes_the_runs_directory():
    """Each resolve_standing_facts call in runner.py passes ``drivers=drivers``."""
    tree = ast.parse((_REPO / "agents" / "tier2" / "runner.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None)) == "resolve_standing_facts"]
    # CHANGED by FI stage C Group 3 (landing fix L2): main's three handlers plus recall and
    # warranty, each of which now passes its own directory.
    # CHANGED by FI stage D task 3.8: service adds a sixth handler call site.
    assert len(calls) == 6, f"expected the six handler call sites, found {len(calls)}"
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert isinstance(kw.get("drivers"), ast.Name) and kw["drivers"].id == "drivers", (
            f"runner.py:{c.lineno} must pass drivers=drivers"
        )


def test_runner_directory_is_off_without_a_pool_and_regional_with_one(monkeypatch):
    from agents.tier2 import runner

    made: list[tuple[str, Optional[str]]] = []

    class _FakeBoto3:
        @staticmethod
        def client(service: str, region_name: Optional[str] = None) -> object:
            made.append((service, region_name))
            return object()

    monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3)
    monkeypatch.delenv("CMS_USER_POOL_ID", raising=False)
    monkeypatch.delenv("CMS_DRIVERS_TABLE", raising=False)
    off = runner._assigned_driver_directory("staging", ddb_client=_DriversTable([]))
    assert off.lookup(VEHICLE) == (None, "driver_directory_unconfigured")
    assert made == []

    monkeypatch.setenv("CMS_USER_POOL_ID", "eu-west-1_TestR1")
    monkeypatch.setenv("CMS_DRIVERS_TABLE", "cms-staging-storage-drivers")
    on = runner._assigned_driver_directory("staging", ddb_client=_DriversTable([]))
    assert made == [("cognito-idp", "eu-west-1")]
    assert on._user_pool_id == "eu-west-1_TestR1"
    assert on._drivers_table == "cms-staging-storage-drivers"



# ---------------------------------------------------------------------------
# Review cycle 1 (C1): each sub stays on its own vehicle, and nothing outlives a run
# ---------------------------------------------------------------------------

SECOND_VIN = "1G1FY6S07N4100001"
SECOND_VEHICLE = "MRDCW01H8PC00V7RD"
SECOND_EMAIL = "vehicle.owner@example.com"


def test_two_vehicles_in_one_run_resolve_to_their_own_drivers():
    """The normal production path: one directory, several drivers. A cache keyed on
    anything coarser than the email (review mutation R4) puts the first driver on
    every vehicle."""
    registry = _Registry(
        rows={VEHICLE: {"fleetId": {"S": FLEET}}, SECOND_VEHICLE: {"fleetId": {"S": FLEET}}},
        vin_to_vehicle={VIN: VEHICLE, SECOND_VIN: SECOND_VEHICLE},
    )
    d, _, pool = _directory(
        [_driver("DRV-MRDN-0015", VEHICLE, EMAIL), _driver("DRV-VO-001", SECOND_VEHICLE, SECOND_EMAIL)],
        [_user(DRIVER_SUB, EMAIL), _user(OTHER_SUB, SECOND_EMAIL)],
    )
    kw = dict(ddb_client=registry, vehicles_table="t", drivers=d)
    assert OR.resolve_standing_facts(VIN, **kw).owner_id == DRIVER_SUB
    assert OR.resolve_standing_facts(SECOND_VIN, **kw).owner_id == OTHER_SUB
    # And in the other order, from the cache.
    assert OR.resolve_standing_facts(SECOND_VIN, **kw).owner_id == OTHER_SUB
    assert OR.resolve_standing_facts(VIN, **kw).owner_id == DRIVER_SUB
    assert len(pool.calls) == 2


def test_a_verified_user_with_an_unverified_twin_is_ambiguous():
    """The documented case (module docstring): a native verified user plus a federated
    user carrying the same address, unverified. Uniqueness counts every match, so an
    unverified twin blocks rather than being skipped (review mutation R3)."""
    d, _, _ = _directory(
        [_driver("DRV-A", VEHICLE, EMAIL)],
        [_user(DRIVER_SUB, EMAIL), _user(OTHER_SUB, EMAIL, verified="false", status="EXTERNAL_PROVIDER")],
    )
    assert d.lookup(VEHICLE) == (None, "multiple_pool_users")


def test_each_runner_call_builds_a_fresh_directory(monkeypatch):
    """Review mutation R1: memoizing the directory across invocations would keep a
    reassigned vehicle's previous driver as owner_id while the container stays warm."""
    from agents.tier2 import runner

    monkeypatch.setenv("CMS_USER_POOL_ID", "")
    a = runner._assigned_driver_directory("staging", ddb_client=_DriversTable([]))
    b = runner._assigned_driver_directory("staging", ddb_client=_DriversTable([]))
    assert a is not b


def test_every_directory_is_built_inside_the_handler_that_uses_it():
    """Review mutation R2: each handler that resolves standing binds its own
    ``drivers = _assigned_driver_directory(...)``, so no site can pass ``None`` or a
    module-level instance while still naming the keyword ``drivers``."""
    tree = ast.parse((_REPO / "agents" / "tier2" / "runner.py").read_text())

    def _calls(node, name):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", None)) == name]

    # No module-level construction or caching.
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        assert not _calls(stmt, "_assigned_driver_directory"), "module-level directory"
        assert not _calls(stmt, "AssignedDriverDirectory"), "module-level directory"
    helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_assigned_driver_directory")
    assert not helper.decorator_list, "the directory factory must not be cached"

    handlers = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                and _calls(n, "resolve_standing_facts")]
    # CHANGED by FI stage C Group 3 (landing fix L2): named, so a handler can't be swapped.
    # CHANGED by FI stage D task 3.8: service adds _run_service_from_env.
    assert {fn.name for fn in handlers} == {
        "lambda_handler", "_run_diagnostics_from_env", "_run_cost_from_env",
        "_run_recall_from_env", "_run_warranty_from_env", "_run_service_from_env"}
    for fn in handlers:
        binds = [s for s in ast.walk(fn) if isinstance(s, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "drivers" for t in s.targets)]
        assert len(binds) == 1, f"{fn.name}: expected one `drivers = ...` binding"
        value = binds[0].value
        assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == "_assigned_driver_directory", (
            f"{fn.name}: drivers must be bound to _assigned_driver_directory(...)"
        )
