"""The FG15 live check applies the same VIN rule as the Tier 2 writer.

Issue ``issues/2026-11-07-standing-dropped-when-vehicleid-equals-vin/``: after the CMS
vehicleId = VIN migration a projected ``vehicle_id`` equal to the VIN is correct, while
``owner_id`` and ``fleet_id`` must still never be the VIN. If the live check kept the old
rule it would fail on every moved car, and the easy "fix" would be to restore the bug.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "live_verify_fg15", _REPO / "scripts" / "live-verify-fg15-owner-projection.py")
fg15 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fg15)

from agents.tier2 import writer as W  # noqa: E402

VIN = "MRDN0000001000080"


def test_the_live_check_and_the_writer_allow_the_same_attributes():
    assert fg15.MAY_EQUAL_VIN == W._MAY_EQUAL_SCOPE_VIN
    assert set(fg15.PROJECTED) == set(W._STANDING_ATTRS)


def test_vehicle_id_equal_to_the_vin_is_not_a_leak():
    assert fg15.vin_leak("vehicle_id", VIN, VIN) is False


@pytest.mark.parametrize("attr", ["owner_id", "fleet_id"])
def test_owner_or_fleet_equal_to_the_vin_is_a_leak(attr):
    assert fg15.vin_leak(attr, VIN, VIN) is True


@pytest.mark.parametrize("attr", ["owner_id", "fleet_id", "vehicle_id"])
def test_blank_is_a_leak(attr):
    assert fg15.vin_leak(attr, "", VIN) is True


@pytest.mark.parametrize("attr", ["owner_id", "fleet_id", "vehicle_id"])
def test_absent_or_other_values_are_not_leaks(attr):
    assert fg15.vin_leak(attr, None, VIN) is False
    assert fg15.vin_leak(attr, "something-else", VIN) is False


# ---------------------------------------------------------------------------
# The functions Part A and Part C call (review cycle 2 W1)
# ---------------------------------------------------------------------------

from agents.tier2.owner_resolver import StandingFacts  # noqa: E402

SUB = "3333cccc-4444-4555-8666-7777dddd7780"
FLEET = "demo-city-public"
MOVED = StandingFacts(SUB, FLEET, VIN)


def test_part_a_accepts_a_moved_car_resolving_to_its_vin():
    assert fg15.resolver_mismatch(MOVED, MOVED, MOVED, VIN) is False


@pytest.mark.parametrize("got", [
    pytest.param(StandingFacts(VIN, FLEET, VIN), id="owner-is-the-vin"),
    pytest.param(StandingFacts(SUB, VIN, VIN), id="fleet-is-the-vin"),
    pytest.param(StandingFacts(SUB, FLEET, ""), id="blank-vehicle-id"),
])
def test_part_a_flags_a_leak_even_when_the_oracle_agrees(got):
    assert fg15.resolver_mismatch(got, got, got, VIN) is True


def test_part_a_flags_disagreement_and_instability():
    other = StandingFacts(None, FLEET, VIN)
    assert fg15.resolver_mismatch(other, MOVED, other, VIN) is True
    assert fg15.resolver_mismatch(MOVED, MOVED, other, VIN) is True


def test_part_c_accepts_a_moved_car_item_with_vehicle_id_equal_to_the_vin():
    item = {"owner_id": SUB, "fleet_id": FLEET, "vehicle_id": VIN}
    assert fg15.projection_mismatches(item, MOVED, VIN) == []


@pytest.mark.parametrize("attr", ["owner_id", "fleet_id"])
def test_part_c_flags_owner_or_fleet_equal_to_the_vin_even_when_the_oracle_says_so(attr):
    want = MOVED._replace(**{attr: VIN})
    item = {"owner_id": want.owner_id, "fleet_id": want.fleet_id, "vehicle_id": VIN}
    assert fg15.projection_mismatches(item, want, VIN) == [attr]


def test_part_c_flags_a_missing_or_extra_projection():
    assert fg15.projection_mismatches({"owner_id": SUB, "fleet_id": FLEET}, MOVED, VIN) == ["vehicle_id"]
    assert fg15.projection_mismatches({"vehicle_id": VIN}, StandingFacts(), VIN) == ["vehicle_id"]


NOW = "2026-11-07T20:00:00Z"
SINCE = "2026-11-07T19:00:00Z"


@pytest.mark.parametrize("item,expected", [
    ({"computed_at": "2026-11-07T19:05:00Z", "expires_at": "2026-11-07T20:30:00Z"}, True),
    ({"computed_at": "2026-11-07T19:05:00.123+00:00", "expires_at": "2026-11-15T19:05:00Z"}, True),
    ({"computed_at": "2026-11-07T18:55:00Z", "expires_at": "2026-11-15T18:55:00Z"}, False),
    ({"computed_at": "2026-11-07T19:05:00Z", "expires_at": "2026-11-07T19:35:00Z"}, False),
    ({"computed_at": "not-a-time", "expires_at": "2026-11-15T19:05:00Z"}, False),
    ({"expires_at": "2026-11-15T19:05:00Z"}, False),
])
def test_part_c_scope_is_live_rows_written_since(item, expected):
    assert fg15.written_since(item, SINCE, NOW) is expected


def test_part_c_scope_without_since_is_every_row():
    assert fg15.written_since({}, None, NOW) is True


def test_part_c_scope_refuses_a_malformed_since():
    with pytest.raises(ValueError):
        fg15.written_since({"computed_at": SINCE, "expires_at": NOW}, "yesterday", NOW)


# ---------------------------------------------------------------------------
# Part A and Part C driven end to end against fakes of the real tables
# ---------------------------------------------------------------------------
#
# These run the parts themselves, so a call site that stops using the functions above, or
# reverts to the pre-fix rule (`vin in tuple(got)`, `got in ("", vin)`), fails here.

from agents.tier2.owner_resolver import resolve_standing_facts  # noqa: E402

REG = "cms-test-storage-vehicles"
DRV = "cms-test-storage-drivers"
POOL = "us-west-2_TestP3"
EMAIL = "moved.driver@example.com"


class _World:
    """The CMS registry (vin-index + base table) and drivers table, one low-level client."""

    def __init__(self, vin_to_vehicle: dict[str, str], fleets: dict[str, str],
                 drivers: list[tuple[str, str, str]]):
        self.vin_to_vehicle, self.fleets, self.drivers = vin_to_vehicle, fleets, drivers

    def query(self, **kw):
        if kw["TableName"] != REG:
            raise RuntimeError("ResourceNotFoundException")
        vin = kw["ExpressionAttributeValues"][":v" if ":v" in kw["ExpressionAttributeValues"] else ":vin"]["S"]
        vid = self.vin_to_vehicle.get(vin)
        return {"Items": [{"vehicleId": {"S": vid}, "vin": {"S": vin}}] if vid else []}

    def get_item(self, **kw):
        vid = kw["Key"]["vehicleId"]["S"]
        if vid not in self.fleets:
            return {}
        vin = next(v for v, i in self.vin_to_vehicle.items() if i == vid)
        return {"Item": {"vehicleId": {"S": vid}, "vin": {"S": vin}, "fleetId": {"S": self.fleets[vid]}}}

    def scan(self, **kw):
        assert kw["TableName"] == DRV
        return {"Items": [{"driverId": {"S": d}, "assignedVehicleId": {"S": v}, "cognitoEmail": {"S": e}}
                          for d, v, e in self.drivers]}


class _Idp:
    def __init__(self, users: dict[str, str]):
        self.users = users

    def _hits(self, flt):
        want = flt.split('"')[1].lower()
        return [{"Username": s, "Enabled": True, "Attributes": [
            {"Name": "sub", "Value": s}, {"Name": "email", "Value": e},
            {"Name": "email_verified", "Value": "true"}]}
            for e, s in self.users.items() if e.lower() == want]

    def list_users(self, **kw):
        return {"Users": self._hits(kw["Filter"])}

    def get_paginator(self, name):
        assert name == "list_users"
        idp = self

        class _P:
            def paginate(self, **kw):
                yield {"Users": idp._hits(kw["Filter"])}
        return _P()


@pytest.fixture()
def moved_world(monkeypatch):
    """One moved car (vehicleId == VIN) with one CMS-pool driver; one car with no driver."""
    other = "MRDN0000001000081"
    world = _World({VIN: VIN, other: other}, {VIN: FLEET, other: FLEET}, [("DRV-1", VIN, EMAIL)])
    monkeypatch.setattr(fg15.boto3, "client", lambda *a, **k: _Idp({EMAIL: SUB}))
    monkeypatch.setattr(fg15, "failures", [])
    findings = [{"finding_id": "AVX-FND-a", "scope": {"vin": VIN}},
                {"finding_id": "AVX-FND-b", "scope": {"vin": other}}]
    return world, findings


def test_part_a_passes_for_a_moved_car(moved_world):
    world, findings = moved_world
    truth = fg15.part_a(findings, world, REG, DRV, POOL, "us-west-2")
    assert fg15.failures == []
    assert truth[VIN] == StandingFacts(SUB, FLEET, VIN)


def test_part_a_fails_on_the_pre_fix_resolver(moved_world):
    world, findings = moved_world

    def pre_fix(vin, **kw):  # the 2026-11-07 regression: vehicle_id and owner dropped
        return resolve_standing_facts(vin, **kw)._replace(vehicle_id=None, owner_id=None)

    fg15.part_a(findings, world, REG, DRV, POOL, "us-west-2", resolver=pre_fix)
    assert any("resolver == oracle" in f for f in fg15.failures), fg15.failures


class _Res:
    """Resource-style table: by_vin / by_owner queries answered from the items."""

    def __init__(self, items):
        self.items = items

    def Table(self, _name):  # noqa: N802 (boto3 name)
        res = self

        class _T:
            def query(self, IndexName, KeyConditionExpression):  # noqa: N803
                expr = KeyConditionExpression.get_expression()
                name, value = expr["values"][0].name, expr["values"][1]
                return {"Items": [i for i in res.items if i.get(name) == value]}
        return _T()


def _item(**attrs):
    base = {"finding_id": "AVX-FND-a", "scope": {"vin": VIN}, "vin": VIN}
    base.update(attrs)
    return base


def test_part_c_passes_for_a_converged_moved_car_item(monkeypatch):
    monkeypatch.setattr(fg15, "failures", [])
    item = _item(owner_id=SUB, fleet_id=FLEET, vehicle_id=VIN)
    fg15.part_c([item], {VIN: MOVED}, _Res([item]), "t")
    assert fg15.failures == []


@pytest.mark.parametrize("attrs", [
    pytest.param({"owner_id": SUB, "fleet_id": FLEET}, id="vehicle_id-missing"),
    pytest.param({"fleet_id": FLEET, "vehicle_id": VIN}, id="owner-missing"),
])
def test_part_c_fails_for_an_unconverged_item(monkeypatch, attrs):
    monkeypatch.setattr(fg15, "failures", [])
    item = _item(**attrs)
    fg15.part_c([item], {VIN: MOVED}, _Res([item]), "t")
    assert any("projection matches the oracle" in f for f in fg15.failures), fg15.failures


# ---------------------------------------------------------------------------
# Review cycle 3 suggestions: the CLI scope and the Part A floor
# ---------------------------------------------------------------------------


def _row(vin, computed, expires="2026-11-15T19:05:00Z"):
    return {"finding_id": f"F-{vin}-{computed}", "scope": {"vin": vin},
            "computed_at": computed, "expires_at": expires}


def test_scope_findings_applies_written_since_and_vins():
    rows = [_row(VIN, "2026-11-07T19:05:00Z"), _row(VIN, "2026-11-07T18:00:00Z"),
            _row("MRDN0000001000082", "2026-11-07T19:05:00Z")]
    kept = fg15.scope_findings(rows, SINCE, VIN, NOW)
    assert [r["finding_id"] for r in kept] == [f"F-{VIN}-2026-11-07T19:05:00Z"]
    assert len(fg15.scope_findings(rows, None, None, NOW)) == 3


def test_scope_findings_keeps_a_row_written_exactly_at_since():
    assert len(fg15.scope_findings([_row(VIN, SINCE)], SINCE, None, NOW)) == 1


def test_scope_findings_refuses_a_malformed_since_even_with_no_rows():
    with pytest.raises(ValueError):
        fg15.scope_findings([], "yesterday", None, NOW)


def test_main_scopes_through_scope_findings():
    """main() must call the tested function, not an inline copy that could drift."""
    import inspect
    src = inspect.getsource(fg15.main)
    assert "scope_findings(findings, args.written_since, args.vins, now_iso)" in src


def test_part_a_fails_when_no_vin_resolves_vehicle_id_equal_to_the_vin(monkeypatch):
    """The floor that shows moved cars were checked: a registry of surrogate ids fails it."""
    world = _World({VIN: "VEH-TEST-0082"}, {"VEH-TEST-0082": FLEET},
                   [("DRV-1", "VEH-TEST-0082", EMAIL)])
    monkeypatch.setattr(fg15.boto3, "client", lambda *a, **k: _Idp({EMAIL: SUB}))
    monkeypatch.setattr(fg15, "failures", [])
    fg15.part_a([{"finding_id": "F", "scope": {"vin": VIN}}], world, REG, DRV, POOL, "us-west-2")
    assert any("vehicle_id == VIN" in f for f in fg15.failures), fg15.failures
