"""Service-lane contract pins: DMS is_fi_fleet_health_booking predicate (task 1.4).

Existing coverage — not duplicated here, cited for the mutant record:
  test_labor_op_vocabulary.py::test_every_bookable_fault_code_books_an_op_adp_counts
    — every code+band (including p0) books a labor_op listed in ADP's
      adp_code_labor_op_map.json for that code.  Catches mutant-1 (LAB-ELE-07).
  test_fleet_service_need_id.py::test_every_sent_line_carries_its_needs_need_id_in_order
    — _fleet_service_body sends avx_action_id (bare UUID) and need_id on every line.

New in this file:
  TestDmsPredicateBothBranches
    Ports DMS:source/simulation/fi_stand_ins.py::is_fi_fleet_health_booking
    (commit 1dbbf01659e031d19bf5d0a799c4aed9d376c679, lines 56-84) verbatim,
    then asserts that _fleet_service_body's output (plus customer_type="fleet",
    which DMS's fleet route adds when storing the RO) satisfies it for every p0
    code.  Also exercises both False branches of the predicate explicitly.

    Additionally checks that each p0 booking line's labor_op is in ADP's map for
    that code — this is already covered by test_labor_op_vocabulary, but is included
    here so that mutant-1 (changing a p0 code to LAB-ELE-07) also fails this file.

Mutant record: .kiro/specs/2026-11-02-fi-ground-book-return/records/1.4-mutants.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.executors.dms import _fleet_service_body
from agents.fi_fleet_health import loop

_DIR = Path(loop.__file__).resolve().parent
_VENDORED = _DIR / "adp_code_labor_op_map.json"

# ---------------------------------------------------------------------------
# DMS predicate — ported verbatim
# DMS:source/simulation/fi_stand_ins.py commit 1dbbf01659e031d19bf5d0a799c4aed9d376c679
# Both branches preserved exactly as written in that file.
# ---------------------------------------------------------------------------

def _is_fi_avx_fleet_ro(ro: dict) -> bool:
    """An AVX booking on DMS's fleet route: avx_action_id set, fleet customer.

    DMS:source/simulation/fi_stand_ins.py:56-59, commit 1dbbf016.
    """
    return bool(ro.get("avx_action_id")) and ro.get("customer_type") == "fleet"


def _port_is_fi_fleet_health_booking(ro: dict) -> bool:
    """True only for an RO created by an FI fleet-health booking (Stage D, spec D9).

    Port of DMS:source/simulation/fi_stand_ins.py:72-84,
    commit 1dbbf01659e031d19bf5d0a799c4aed9d376c679.
    Both branches (the _is_fi_avx_fleet_ro short-circuit and the any() over lines)
    preserved verbatim.
    """
    if not _is_fi_avx_fleet_ro(ro):
        return False
    return any(
        isinstance(ln.get("need_id"), str) and ln["need_id"].strip() != ""
        for ln in ro.get("lines") or []
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _adp_map() -> dict:
    return json.loads(_VENDORED.read_text(encoding="utf-8"))


def _p0_codes(adp_map: dict) -> frozenset[str]:
    """P0 codes = fault_codes in ADP's map whose listed op is unique to that one code
    and does not appear in system_repair_ops.

    From adp_code_labor_op_map.json _doc[3]:
      'Each p0 (stop-driving) code has an op of its own, listed for no other code or task.'
    """
    system_ops: set[str] = set(adp_map.get("system_repair_ops", {}).values())
    fault_codes: dict[str, list[str]] = adp_map.get("fault_codes", {})
    # Count across all codes how many codes use each non-system op.
    op_count: dict[str, int] = {}
    for ops in fault_codes.values():
        for op in ops:
            if op not in system_ops:
                op_count[op] = op_count.get(op, 0) + 1
    # A code is p0 iff its first op is non-system and used by exactly this one code.
    return frozenset(
        code
        for code, ops in fault_codes.items()
        if ops and ops[0] not in system_ops and op_count.get(ops[0], 0) == 1
    )


_ACTION_UUID = "7e9f2a4b-1c3d-4e5f-8a7b-0d1e2f3a4b5c"
_VIN = "TESTVIN0000000001"
_DEALER = "dealer-test-01"


def _make_action(code: str, cfg: dict) -> dict[str, Any]:
    """Minimal book_fleet_service action for one fault code at band p0."""
    mapped = loop.map_need_to_labor_op(
        {"kind": "fault", "code": code, "severity_band": "p0"}, cfg
    )
    return {
        "action_id": f"AVX-ACT-{_ACTION_UUID}",
        "action_kind": "book_fleet_service",
        "parameters": {
            "vehicle_vin": _VIN,
            "dealer_id": _DEALER,
            "needs": [
                {
                    "need_id": f"fault:{code}",
                    "labor_op": mapped["labor_op"],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDmsPredicateBothBranches:
    """Port of DMS is_fi_fleet_health_booking exercised against _fleet_service_body output."""

    def test_p0_codes_catalog_is_non_empty(self) -> None:
        """Sanity: the ADP map contains the 10 known p0 codes from research Q2."""
        adp_map = _adp_map()
        p0 = _p0_codes(adp_map)
        expected = frozenset({
            "B0001_FIRE", "C1234", "P0217", "P0520", "P0700",
            "P0A0D", "P0A80", "P0AA6", "U3000", "U3000_CRASH",
        })
        assert p0 == expected, (
            f"p0 codes mismatch — catalog may have changed; "
            f"extra={p0 - expected}, missing={expected - p0}"
        )

    def test_p0_body_satisfies_dms_fi_fleet_health_booking(self) -> None:
        """Every p0 code: _fleet_service_body + customer_type='fleet' satisfies predicate.

        customer_type is set by DMS's fleet route when storing the RO; AVX's executor
        does not include it in the HTTP request.  This test simulates the stored RO.

        Both predicate branches are verified:
          Branch 1: avx_action_id truthy AND customer_type == 'fleet'
          Branch 2: any line has a non-empty string need_id

        Also asserts each booking line's labor_op is in ADP's map for that code.
        This assertion is already covered by
        test_labor_op_vocabulary::test_every_bookable_fault_code_books_an_op_adp_counts
        but is included here so that mutant-1 (LAB-ELE-07 for a p0 code) also fails
        this file without needing to run the vocabulary suite.

        Catches mutant-2: drop need_id from a line in _fleet_service_body →
          branch 2's any() finds no line with need_id → predicate returns False →
          assert _port_is_fi_fleet_health_booking(ro) fails.
        """
        adp_map = _adp_map()
        fault_codes_map: dict[str, list[str]] = adp_map["fault_codes"]
        p0 = _p0_codes(adp_map)
        cfg = loop.load_needs_config()

        for code in sorted(p0):
            action = _make_action(code, cfg)
            body = _fleet_service_body(action)
            assert body is not None, f"_fleet_service_body returned None for {code}"

            # Simulate DMS-stored RO: fleet route adds customer_type="fleet".
            ro: dict[str, Any] = {**body, "customer_type": "fleet"}

            # --- Branch 1 -------------------------------------------------------
            assert ro.get("avx_action_id"), f"avx_action_id missing in stored RO for {code}"
            assert ro.get("customer_type") == "fleet", (
                f"customer_type not 'fleet' in stored RO for {code}"
            )

            # --- Branch 2 -------------------------------------------------------
            lines: list[dict] = ro.get("lines") or []
            assert any(
                isinstance(ln.get("need_id"), str) and ln["need_id"].strip()
                for ln in lines
            ), f"no line with non-empty need_id for {code}"

            # --- Full predicate -------------------------------------------------
            assert _port_is_fi_fleet_health_booking(ro), (
                f"is_fi_fleet_health_booking returned False for {code}"
            )

            # --- Labor op in ADP's map (also catches mutant-1: LAB-ELE-07) ------
            assert code in fault_codes_map, f"{code} not in ADP fault_codes map"
            line_ops = {ln.get("labor_op") for ln in lines if ln.get("labor_op")}
            adp_ops_for_code = set(fault_codes_map[code])
            assert line_ops & adp_ops_for_code, (
                f"{code}: line labor_ops {line_ops} share no entry with "
                f"ADP's map {adp_ops_for_code}"
            )

    # ---- Negative branches ---------------------------------------------------

    def test_branch1_fails_without_avx_action_id(self) -> None:
        """Branch 1 short-circuits: missing avx_action_id → _is_fi_avx_fleet_ro False."""
        ro = {
            "customer_type": "fleet",
            "lines": [{"need_id": "fault:C1234", "labor_op": "LAB-BRKSYS-01"}],
        }
        # _is_fi_avx_fleet_ro: bool(ro.get("avx_action_id")) is False → returns False
        assert not _port_is_fi_fleet_health_booking(ro)

    def test_branch1_fails_wrong_customer_type(self) -> None:
        """Branch 1 short-circuits: customer_type != 'fleet' → _is_fi_avx_fleet_ro False."""
        ro = {
            "avx_action_id": _ACTION_UUID,
            "customer_type": "retail",
            "lines": [{"need_id": "fault:C1234", "labor_op": "LAB-BRKSYS-01"}],
        }
        # _is_fi_avx_fleet_ro: ro.get("customer_type") == "fleet" is False → returns False
        assert not _port_is_fi_fleet_health_booking(ro)

    def test_branch2_fails_when_no_line_has_need_id(self) -> None:
        """Branch 2: fleet AVX RO with no need_id on any line → False."""
        ro = {
            "avx_action_id": _ACTION_UUID,
            "customer_type": "fleet",
            "lines": [{"line_no": 1, "labor_op": "LAB-BRKSYS-01", "pay_type": "customer_pay"}],
        }
        # any(isinstance(ln.get("need_id"), str) ...) → False (need_id absent on all lines)
        assert not _port_is_fi_fleet_health_booking(ro)

    def test_branch2_fails_when_need_id_is_whitespace(self) -> None:
        """Branch 2: need_id present but whitespace-only does not satisfy the predicate."""
        ro = {
            "avx_action_id": _ACTION_UUID,
            "customer_type": "fleet",
            "lines": [{"need_id": "   ", "labor_op": "LAB-BRKSYS-01"}],
        }
        # ln["need_id"].strip() != "" is False → any() is False
        assert not _port_is_fi_fleet_health_booking(ro)
