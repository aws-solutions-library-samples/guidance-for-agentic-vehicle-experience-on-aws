"""Contract test: `api-drivers-me._load_vehicle` vs `api-vehicle-context._load_vehicle`.

WHY THIS EXISTS
---------------
`api-drivers-me._load_vehicle`'s docstring has always claimed:

    "Same curated subset as api-vehicle-context so responses are consistent
     across both endpoints."

Nothing enforced it, and it drifted three times:

- 2026-07-29  `totalTrips` / `purchaseDate` / `purchasePrice` / `enrollmentStatus`
              were absent, so the trade-in condition score silently dropped its
              distance-for-age and usage-pattern factors.
- 2026-08-19  `batterySoh` / `batteryCapacityKwh` were added to vehicle-context
              for the EV battery card, and NOT to this sibling.
- 2026-08-21  Consequence surfaced: iOS reads `batterySoh` from
              `session.currentVehicle`, populated by THIS endpoint, so a vehicle
              whose row carries `batterySoh: 94` rendered "—" in the Home stat
              cell, the Vehicle-tab signal row, and the battery card.

Every instance shares one failure mode: these projections are
**denylists-by-omission**, and an absent optional decodes cleanly in Swift. No
error, no log line, nothing observable from the client. The field is simply
never there.

A contract asserted in a docstring is not a contract. This test is the link.

APPROACH
--------
Source-parses both handlers with `ast` instead of importing them, so it needs no
boto3, no AWS credentials and no network — same philosophy as the sibling
`api-tenant-config/tests/test_ios_tenant_config_contract.py`.

Parity is asserted in the direction the contract actually requires: every field
vehicle-context exposes must also be exposed here. The reverse is NOT required —
see `test_extra_fields_here_are_allowed`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_LAMBDAS = Path(__file__).resolve().parents[2]
_THIS = _LAMBDAS / "api-drivers-me" / "handler.py"
_SIBLING = _LAMBDAS / "api-vehicle-context" / "handler.py"


def _projected_fields(handler_path: Path) -> set[str]:
    """Field names in `_load_vehicle`'s curated allowlist.

    The allowlist is the tuple literal iterated by the returned dict
    comprehension. Collecting every string constant in the function body would
    also pick up the docstring and the DDB attribute names, so this walks to the
    comprehension specifically.
    """
    tree = ast.parse(handler_path.read_text())
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_load_vehicle"
        ),
        None,
    )
    assert fn is not None, f"_load_vehicle not found in {handler_path}"

    comps = [n for n in ast.walk(fn) if isinstance(n, ast.DictComp)]
    assert comps, f"no dict comprehension in {handler_path}:_load_vehicle"

    fields: set[str] = set()
    for comp in comps:
        for gen in comp.generators:
            for const in ast.walk(gen.iter):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    fields.add(const.value)
    assert fields, f"no field names parsed from {handler_path}"
    return fields


@pytest.fixture(scope="module")
def here() -> set[str]:
    return _projected_fields(_THIS)


@pytest.fixture(scope="module")
def sibling() -> set[str]:
    return _projected_fields(_SIBLING)


def test_every_vehicle_context_field_is_also_projected_here(here, sibling):
    """The docstring's promise, made executable.

    A field exposed by vehicle-context but not here is invisible to any iOS
    surface reading `session.currentVehicle` — which is populated by
    `/drivers/me`, not by `/vehicle-context`.
    """
    missing = sibling - here
    assert not missing, (
        "api-vehicle-context projects fields that api-drivers-me drops, so they "
        "arrive nil on iOS and fail silently:\n"
        + "".join(f"  - {f}\n" for f in sorted(missing))
        + "\nAdd them to api-drivers-me/_load_vehicle, or narrow vehicle-context."
    )


def test_extra_fields_here_are_allowed(here, sibling):
    """Documents the asymmetry rather than asserting strict equality.

    `/drivers/me` is the driver-scoped endpoint, so it may legitimately expose
    fields vehicle-context has no reason to. Strict equality would fail on a
    harmless addition and train people to edit the test instead of the bug.
    """
    assert here >= sibling


@pytest.mark.parametrize("field", ["batterySoh", "batteryCapacityKwh"])
def test_ev_battery_fields_are_projected(here, field):
    """Regression pin for 2026-08-21.

    These two are called out by name because the symptom — a battery card
    reading "—" on a vehicle with a healthy 94% pack — is indistinguishable
    from a genuinely absent reading, which is exactly why it went unnoticed.
    """
    assert field in here, (
        f"{field} missing from api-drivers-me/_load_vehicle; iOS renders the "
        f"battery health cell as an em dash when it arrives nil"
    )


def test_state_of_charge_is_not_duplicated(here):
    """`fuelLevel` carries SoC for a BEV (the backend aliases Redis `ev_soc`
    onto it), so a second SoC field would create two sources for one number.
    Pins the note in vehicle-context's allowlist.
    """
    assert "fuelLevel" in here
    assert not {"batterySoc", "batterySOC", "stateOfCharge"} & here
