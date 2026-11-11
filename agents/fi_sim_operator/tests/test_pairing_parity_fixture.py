"""Parity fixture test: AVX pair_booking against pairing_cases.json (spec D1, D10b, task 11.6e).

Verifies that AVX's ``pair_booking`` agrees with the expected outcomes in the shared fixture
file.  The fixture file is byte-for-byte identical in CMS; both tests pin the same sha256.

Run from the project root:
    .venv/bin/python -m pytest -p no:cacheprovider -q \\
        agents/fi_sim_operator/tests/test_pairing_parity_fixture.py \\
        agents/fi_sim_operator/tests/test_pairing.py
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from agents.fi_sim_operator.pairing import pair_booking

# ── fixture ────────────────────────────────────────────────────────────────────

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "pairing_cases.json"

# sha256 of the fixture file as written; also pinned in CMS test_fi_ground_book_parity.py
SHA256_PIN = "aca14b8dacb90b6ff805ba051dae3d4e5142a1573e5214441aa812ba8dd4a0d4"

# ── helpers ────────────────────────────────────────────────────────────────────


def _load() -> dict:
    return json.loads(FIXTURE_PATH.read_bytes())


def _run_avx(case: dict, data: dict) -> tuple:
    return pair_booking(
        case["grounding"],
        case["candidates"],
        set(case["decided_finding_ids"]),
        data["now_iso"],
    )


# ── sha256 pin ─────────────────────────────────────────────────────────────────


def test_fixture_sha256_unchanged() -> None:
    """Fixture bytes must match the pinned sha256; update both tests on intentional change."""
    actual = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert actual == SHA256_PIN, (
        f"pairing_cases.json changed (got {actual}); "
        "update SHA256_PIN in both test_pairing_parity_fixture.py AND "
        "test_fi_ground_book_parity.py"
    )


# ── parametrised parity tests ─────────────────────────────────────────────────

_DATA = _load()
_CASES = _DATA["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_avx_pairing_case(case: dict) -> None:
    """AVX pair_booking must return the expected outcome for every fixture case."""
    booking, reason = _run_avx(case, _DATA)

    # Cases with a known difference use avx_expected; all others use expected.
    exp = case.get("avx_expected") or case["expected"]
    exp_fid: str | None = exp["booking_finding_id"]
    exp_reason: str | None = exp["reason"]

    known_diff = case.get("known_difference")
    label = f"[{case['name']}]"
    if known_diff:
        label += f" [known_difference: {known_diff}]"

    assert reason == exp_reason, f"{label} reason: got {reason!r}, want {exp_reason!r}"
    if exp_fid is None:
        assert booking is None, f"{label} booking should be None, got {booking!r}"
    else:
        assert booking is not None, f"{label} booking should not be None"
        assert booking["finding_id"] == exp_fid, (
            f"{label} booking_finding_id: got {booking['finding_id']!r}, want {exp_fid!r}"
        )


# ── M1 mutant: AVX ignores disclosure_txt ─────────────────────────────────────


def test_m1_mutant_disclosure_ignored_detected() -> None:
    """M1: if disclosure_txt check is bypassed, the fixture's disclosure_differs case catches it.

    We simulate the mutant by forcing both grounding and booking to share the same
    disclosure_txt (as if the check were removed) and confirm the result changes.
    """
    data = _load()
    case = next(c for c in data["cases"] if c["name"] == "disclosure_differs")

    # Normal call: must return disclosure_differs
    _, reason_normal = pair_booking(
        case["grounding"], case["candidates"],
        set(case["decided_finding_ids"]), data["now_iso"],
    )
    assert reason_normal == "disclosure_differs", (
        "control failed: disclosure_differs case must return disclosure_differs"
    )

    # M1 mutant: make both disclosure_txt identical so the check is bypassed
    g_m1 = {**case["grounding"], "disclosure_txt": "FORCED-MATCH"}
    cands_m1 = [{**c, "disclosure_txt": "FORCED-MATCH"} for c in case["candidates"]]
    _, reason_mutant = pair_booking(
        g_m1, cands_m1,
        set(case["decided_finding_ids"]), data["now_iso"],
    )
    assert reason_mutant is None, (
        f"M1 mutant should produce None reason (pair succeeds); got {reason_mutant!r}"
    )
    assert reason_mutant != reason_normal, (
        "M1 not detected: removing disclosure check did not change the result"
    )
