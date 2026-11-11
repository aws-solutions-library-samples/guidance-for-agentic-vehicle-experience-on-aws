"""The held car keeps the stand-in running across the vehicleId = VIN re-key (prompt 305).

``scripts/set-fi-sim-operator.py``'s default holds VEH-MRDN-0015 by VIN and by its VIN-form id. The
real ``_resolve_vehicle_ids_to_vins`` runs here against a fake CMS registry shaped as it is before
cohort 3 (row ``VEH-MRDN-0015``, vin ``MRDN0000000000015``) and after it (row ``MRDN0000000000015``).
Both must resolve the default to that VIN, so no fleet is stopped; a non-VIN id with no row still
stops every fleet.

Mutants (recorded in records/11/mutants-11.6-hold.md):
- H1 the default lists ``VEH-MRDN-0015`` again: ``test_default_holds_resolve_after_the_rekey`` fails.
- H2 drop the VIN-shaped fallback: ``test_default_holds_resolve_before_the_rekey`` fails.
- H3 the fallback admits any id: ``test_non_vin_id_with_no_row_still_stops`` fails.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_LAMBDA_ROOT = _HERE.parents[1]
_REPO_ROOT = _HERE.parents[3]
for _p in (str(_LAMBDA_ROOT), str(_LAMBDA_ROOT.parent), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
for _k, _v in {"FI_SIM_OPERATOR_ENABLED": "true", "AVX_SIM_OPERATOR_TABLE": "t", "AVX_ACTIONS_TABLE": "t",
               "AVX_FINDINGS_TABLE": "t", "AVX_AGENT_RUNS_TABLE": "t", "FI_SIM_OPERATOR_PARAM": "/p",
               "CMS_VEHICLES_TABLE": "cms-test-storage-vehicles",
               "CMS_GROUNDINGS_TABLE": "cms-test-storage-vehicle-groundings"}.items():
    os.environ.setdefault(_k, _v)

import handler as h  # noqa: E402

_spec = importlib.util.spec_from_file_location("set_fi_sim_operator_h", _REPO_ROOT / "scripts" / "set-fi-sim-operator.py")
setmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setmod)

VIN = "MRDN0000000000015"


class _Registry:
    """``get_item`` on the vehicles table, keyed by ``vehicleId``, as the real call is made."""

    def __init__(self, rows: dict[str, str]):
        self.rows, self.calls = rows, []

    def get_item(self, *, TableName, Key, ProjectionExpression, ConsistentRead):
        self.calls.append((TableName, Key["vehicleId"]["S"], ConsistentRead))
        vid = Key["vehicleId"]["S"]
        if vid not in self.rows:
            return {}
        return {"Item": {"vehicleId": {"S": vid}, "vin": {"S": self.rows[vid]}}}


def _resolve(monkeypatch, rows):
    reg = _Registry(rows)
    monkeypatch.setattr(h, "_get_ddb", lambda: reg)
    ids = frozenset(setmod.DEFAULT_HOLD_VEHICLE_IDS)
    return h._resolve_vehicle_ids_to_vins(ids, "cms-test-storage-vehicles"), reg


def test_default_holds_resolve_before_the_rekey(monkeypatch):
    got, reg = _resolve(monkeypatch, {"VEH-MRDN-0015": VIN, "VEH-OTHER-0001": "MRDN0000000000001"})
    assert set(got.values()) == {VIN}
    assert all(c[2] is True for c in reg.calls)


def test_default_holds_resolve_after_the_rekey(monkeypatch):
    got, _ = _resolve(monkeypatch, {VIN: VIN, "MRDN0000000000001": "MRDN0000000000001"})
    assert set(got.values()) == {VIN}


def test_default_value_holds_the_car_by_vin_too():
    assert VIN in setmod.DEFAULT_HOLD_VINS and VIN in setmod.DEFAULT_HOLD_VEHICLE_IDS


def test_non_vin_id_with_no_row_still_stops(monkeypatch):
    reg = _Registry({VIN: VIN})
    monkeypatch.setattr(h, "_get_ddb", lambda: reg)
    with pytest.raises(RuntimeError, match="VEH-MRDN-0015"):
        h._resolve_vehicle_ids_to_vins(frozenset({"VEH-MRDN-0015"}), "cms-test-storage-vehicles")


@pytest.mark.parametrize("vid", ["MRDN000000000001", "MRDN00000000000150", "MRDN000000000001O", "mrdn0000000000015"])
def test_almost_vins_with_no_row_still_stop(monkeypatch, vid):
    reg = _Registry({})
    monkeypatch.setattr(h, "_get_ddb", lambda: reg)
    with pytest.raises(RuntimeError):
        h._resolve_vehicle_ids_to_vins(frozenset({vid}), "cms-test-storage-vehicles")


def test_a_row_without_a_vin_still_stops_even_for_a_vin_shaped_id(monkeypatch):
    class _NoVin(_Registry):
        def get_item(self, **kw):
            return {"Item": {"vehicleId": {"S": kw["Key"]["vehicleId"]["S"]}}}
    monkeypatch.setattr(h, "_get_ddb", lambda: _NoVin({}))
    with pytest.raises(RuntimeError):
        h._resolve_vehicle_ids_to_vins(frozenset({VIN}), "cms-test-storage-vehicles")
