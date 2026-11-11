"""
Item 4 of task 1.4 — idempotency and inputs_hash (D11).

Spec: .kiro/specs/2026-10-02-fi-cost-stage-b/spec.md § D11

Invariants tested:
  1. Same evidence → inputs_hash is identical across two calls.
  2. inputs_hash is computed over canonical evidence BEFORE the model call and does
     NOT include narration fields.  (C2 fix: test calls compute_inputs_hash on the raw
     bundle including narration-shaped '_' keys — an implementation that hashes all keys
     would produce different hashes and fail here.)
  3. If the live Finding carries the same hash, no model call is made and no write
     happens (duplicates_skipped incremented).
  4. The Finding's stored inputs_hash equals compute_inputs_hash(evidence) pre-call.
     (C2 fix: assert write_calls[0]['inputs_hash'] == expected_pre_call_hash.)
  5. Different evidence → different hash.
  6. Different hash → model IS called and Finding IS written.
  7. No live Finding → model is called (first run for this vehicle).
  8. Round-trip: run once, feed the written Finding back, assert zero calls and zero
     writes on the second run.  (C2 fix: M3 would store hash computed after the call,
     causing 2 calls + 2nd write on the round trip.)

Bedrock is a stub; the repo-root conftest blocks real AWS calls.

Fix Group 1 changes (review cycle 1, C2 / W1c):
  C2 — narration test calls compute_inputs_hash on raw bundles (not pre-stripped);
       write_calls carry an inputs_hash assertion; round-trip test added.
  W1c — _BASE_EVIDENCE completed with all validator-required keys so the model-success
         path through process_vehicle is exercised, not only the fallback.

All tests are xfail(strict=True).  Group 3/4 makes them pass.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# W1c fix: completed with all validator-required and formula-required keys so that
# a valid 'charging_rate'/'shift_to_depot_off_peak' model reply passes the validator
# and estimate_saving succeeds without KeyError.
# W1 (cycle 2): added energy.mean_price_per_kwh / energy.cohort_mean_price_per_kwh for
# charging_rate consistency per decisions item 1 (mean price above cohort required).
_BASE_EVIDENCE = {
    "vin": "MRD-IDEM-VIN-001",
    "fleet_id": "fleet-idem-01",
    "year_month": "2026-08",
    "cost.month": "2026-08",
    "ratio_cohort": 1.7,
    "ratio_own": 1.4,
    # ADP-provided severity (decisions item 2)
    "severity": "soon",
    "cost.cost_per_mile": 0.45,
    "cost.own_baseline": 0.30,
    "cost.cohort_baseline": 0.28,
    # Charging formula inputs (estimate_saving for shift_to_depot_off_peak)
    "energy.public_and_peak_kwh": 400.0,
    "energy.mean_public_peak_price_per_kwh": 0.38,
    "energy.depot_off_peak_price_per_kwh": 0.09,
    # Cause-consistency keys for validator — decisions item 1 charging_rate rule:
    #   mean price per kWh above cohort AND public/peak share above cohort
    "energy.mean_price_per_kwh": 0.35,          # vehicle mean > cohort 0.15
    "energy.cohort_mean_price_per_kwh": 0.15,
    "energy.public_share": 0.75,                # vehicle public share > cohort 0.30
    "energy.cohort_public_share": 0.30,
    "energy.peak_share": 0.40,                  # depot kWh at peak > cohort 0.15
    "energy.cohort_peak_share": 0.15,
    "energy.dc_fast_share": 0.60,               # public kWh at DC fast > cohort 0.25
    "energy.cohort_dc_fast_share": 0.25,
    # Other cost categories
    "cost.maintenance": 200.0,
    "cost.lease": 600.0,
    "cost.insurance": 150.0,
    "cost.miles": 800.0,
    "cost.fleet_median_miles": 1000.0,
}


def _make_bedrock_stub(call_count_tracker: list) -> Any:
    """Return a Bedrock client stub that records calls and returns a valid reply."""

    def _converse(**kwargs):
        call_count_tracker.append(kwargs)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tool-001",
                                "name": "record_diagnosis",
                                "input": {
                                    "cause": "charging_rate",
                                    "action": "shift_to_depot_off_peak",
                                    "evidence_refs": ["energy.public_and_peak_kwh"],
                                    "headline": "High public charging",
                                    "detail": "Shift to depot off-peak to save costs.",
                                    "confidence": "high",
                                },
                            }
                        }
                    ]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }

    stub = MagicMock()
    stub.converse = _converse
    stub.apply_guardrail.return_value = {"action": "NONE"}
    return stub


# ---------------------------------------------------------------------------
# 1. inputs_hash is stable across two calls on the same evidence
# ---------------------------------------------------------------------------


def test_inputs_hash_stable_for_same_evidence():
    """inputs_hash must be identical when called twice on the same evidence."""
    from agents.fi_cost.evidence import compute_inputs_hash  # noqa: F401

    h1 = compute_inputs_hash(_BASE_EVIDENCE)
    h2 = compute_inputs_hash(_BASE_EVIDENCE)
    assert h1 == h2


# ---------------------------------------------------------------------------
# 2. inputs_hash excludes narration — raw bundle passed, not pre-stripped
# C2 fix: call compute_inputs_hash WITHOUT stripping '_' keys so that an
# implementation that hashes all keys (M2 mutant) produces a different hash.
# ---------------------------------------------------------------------------


def test_inputs_hash_excludes_model_narration():
    """Narration fields (headline, detail) must NOT affect inputs_hash.

    C2 fix: both bundles are passed RAW to compute_inputs_hash (no client-side strip).
    A spec-conforming implementation strips '_'-prefixed keys internally → h_a == h_b.
    M2 (hashes every key) → h_a != h_b → test fails as intended.
    """
    from agents.fi_cost.evidence import compute_inputs_hash  # noqa: F401

    evidence_a = dict(_BASE_EVIDENCE)
    # narration-shaped keys that could appear if narration leaked into evidence
    evidence_b = dict(
        _BASE_EVIDENCE,
        _headline="Some narration text",
        _detail="Some longer narration.",
    )
    # Pass raw to compute_inputs_hash — do NOT strip before calling
    h_a = compute_inputs_hash(evidence_a)
    h_b = compute_inputs_hash(evidence_b)
    assert h_a == h_b, (
        "Narration must not affect inputs_hash; the implementation must strip "
        "narration-shaped keys BEFORE hashing, not rely on the caller to strip them"
    )


# ---------------------------------------------------------------------------
# 3. Same hash → no model call, no write
# ---------------------------------------------------------------------------


def test_same_hash_skips_model_call_and_write():
    """If the live Finding already carries the same inputs_hash, skip entirely."""
    from agents.fi_cost.agent import process_vehicle  # noqa: F401
    from agents.fi_cost import evidence as evidence_mod  # noqa: F401

    bedrock_calls: list = []
    bedrock_stub = _make_bedrock_stub(bedrock_calls)
    write_calls: list = []

    def _stub_write_finding(finding, **kw):
        write_calls.append(finding)

    live_hash = evidence_mod.compute_inputs_hash(_BASE_EVIDENCE)

    live_finding = {
        "finding_id": "AVX-FND-test-001",
        "inputs_hash": live_hash,
    }

    stats = process_vehicle(
        evidence=_BASE_EVIDENCE,
        live_finding=live_finding,
        bedrock_client=bedrock_stub,
        write_finding=_stub_write_finding,
    )

    assert len(bedrock_calls) == 0, (
        "No Bedrock call must be made when the live Finding has the same inputs_hash"
    )
    assert len(write_calls) == 0, (
        "No write must happen when the live Finding has the same inputs_hash"
    )
    assert stats.get("duplicates_skipped", 0) >= 1


# ---------------------------------------------------------------------------
# 4. Different evidence → different hash
# ---------------------------------------------------------------------------


def test_different_evidence_gives_different_hash():
    """A change in any evidence value must produce a different inputs_hash."""
    from agents.fi_cost.evidence import compute_inputs_hash  # noqa: F401

    h_base = compute_inputs_hash(_BASE_EVIDENCE)
    h_changed = compute_inputs_hash(
        dict(_BASE_EVIDENCE, **{"cost.cost_per_mile": 0.99})
    )
    assert h_base != h_changed, (
        "Changing an evidence value must change inputs_hash; "
        "otherwise a changed condition would not trigger a re-diagnosis"
    )


# ---------------------------------------------------------------------------
# 5. Different hash → model IS called and Finding IS written; stored hash is pre-call
# C2 fix: assert write_calls[0]["inputs_hash"] == compute_inputs_hash(evidence)
# ---------------------------------------------------------------------------


def test_different_hash_triggers_model_call_and_stores_pre_call_hash():
    """If the live Finding has a different hash, a model call and write must occur.

    C2 fix: the stored inputs_hash must equal compute_inputs_hash(evidence) computed
    here, BEFORE the call.  M3 stores hash(evidence + headline) computed AFTER the call;
    that hash differs from the pre-call hash → this assertion fails for M3.
    """
    from agents.fi_cost.agent import process_vehicle  # noqa: F401
    from agents.fi_cost.evidence import compute_inputs_hash  # noqa: F401

    bedrock_calls: list = []
    bedrock_stub = _make_bedrock_stub(bedrock_calls)
    write_calls: list = []

    def _stub_write_finding(finding, **kw):
        write_calls.append(finding)

    # Compute the expected hash BEFORE calling process_vehicle
    expected_hash = compute_inputs_hash(_BASE_EVIDENCE)

    stale_finding = {
        "finding_id": "AVX-FND-stale-001",
        "inputs_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    }

    process_vehicle(
        evidence=_BASE_EVIDENCE,
        live_finding=stale_finding,
        bedrock_client=bedrock_stub,
        write_finding=_stub_write_finding,
    )

    assert len(bedrock_calls) == 1, (
        "Exactly one Bedrock call must be made when the live Finding has a different "
        "inputs_hash (D11: one call per vehicle; M5c — no break — would make 2 calls here). "
        "W5 fix: changed from >= 1 to == 1."
    )
    assert len(write_calls) == 1, "Exactly one Finding write must occur when the hash differs"

    # C2 fix: the stored hash must equal the pre-call hash over evidence only
    assert write_calls[0].get("inputs_hash") == expected_hash, (
        "The stored inputs_hash must equal compute_inputs_hash(evidence) computed BEFORE "
        "the model call.  M3 computes it after the call (over evidence + headline), which "
        "produces a different value → this assertion catches M3."
    )


# ---------------------------------------------------------------------------
# 6. No live Finding → model is called (first run for this vehicle)
# C2 fix: assert stored inputs_hash == pre-call hash
# ---------------------------------------------------------------------------


def test_no_live_finding_triggers_model_call_and_stores_pre_call_hash():
    """With no existing Finding, a model call must be made.

    C2 fix: the stored inputs_hash must equal the pre-call hash (M3 check).
    """
    from agents.fi_cost.agent import process_vehicle  # noqa: F401
    from agents.fi_cost.evidence import compute_inputs_hash  # noqa: F401

    bedrock_calls: list = []
    bedrock_stub = _make_bedrock_stub(bedrock_calls)
    write_calls: list = []

    def _stub_write_finding(finding, **kw):
        write_calls.append(finding)

    expected_hash = compute_inputs_hash(_BASE_EVIDENCE)

    process_vehicle(
        evidence=_BASE_EVIDENCE,
        live_finding=None,
        bedrock_client=bedrock_stub,
        write_finding=_stub_write_finding,
    )

    assert len(bedrock_calls) == 1, (
        "Exactly one Bedrock call must be made when there is no live Finding. "
        "W5 fix: changed from >= 1 to == 1 (M5c — no break — makes 2 calls per vehicle)."
    )
    assert len(write_calls) == 1, "Exactly one Finding write must occur on first run"
    assert write_calls[0].get("inputs_hash") == expected_hash, (
        "Stored inputs_hash must equal compute_inputs_hash(evidence) pre-call (M3 guard)"
    )


# ---------------------------------------------------------------------------
# 7. Round-trip: run once, feed the written Finding back → zero calls, zero writes
# C2 fix: M3 stores a different hash (computed after the call), so on the second
# run the live_finding's hash does not match → 2 calls + 2nd write instead of 0.
# ---------------------------------------------------------------------------


def test_round_trip_unchanged_evidence_skips_second_run():
    """Run once, feed the written Finding back → second run is a no-op.

    C2 fix: M3 (stores hash over evidence + headline, computed AFTER the call) would
    produce a Finding whose inputs_hash does NOT equal compute_inputs_hash(evidence).
    Therefore on the second run the hashes don't match → M3 makes 2 model calls and
    a 2nd write, where the correct implementation makes 0 calls and 0 writes.
    This test catches that divergence.

    Spec D11: 'a re-run on unchanged inputs writes nothing and makes no model call'.
    """
    from agents.fi_cost.agent import process_vehicle  # noqa: F401

    # ---- Run 1 ----
    calls_1: list = []
    writes_1: list = []
    bedrock_1 = _make_bedrock_stub(calls_1)

    process_vehicle(
        evidence=_BASE_EVIDENCE,
        live_finding=None,
        bedrock_client=bedrock_1,
        write_finding=lambda f, **kw: writes_1.append(f),
    )

    assert len(writes_1) == 1, "Run 1 must write exactly one Finding"
    assert len(calls_1) == 1, (
        "Run 1 must make exactly one Bedrock call. "
        "W5 fix: M5c (no break) makes 2 calls per vehicle; this assertion catches it."
    )
    written_finding = writes_1[0]

    # ---- Run 2: same evidence, feed the written Finding back ----
    calls_2: list = []
    writes_2: list = []
    bedrock_2 = _make_bedrock_stub(calls_2)

    stats = process_vehicle(
        evidence=_BASE_EVIDENCE,
        live_finding=written_finding,
        bedrock_client=bedrock_2,
        write_finding=lambda f, **kw: writes_2.append(f),
    )

    assert len(calls_2) == 0, (
        "Run 2 on unchanged evidence must make zero Bedrock calls; "
        "M3 (hash computed after call) would fail here because its stored hash "
        "differs from compute_inputs_hash(evidence), causing a spurious re-diagnosis"
    )
    assert len(writes_2) == 0, (
        "Run 2 on unchanged evidence must make zero writes"
    )
    assert stats.get("duplicates_skipped", 0) >= 1



# ---------------------------------------------------------------------------
# 8. Number normalisation (D11 fixed formatting) — idempotency perspective
# Complements test_evidence_inputs.py; tests hash stability from idempotency lens.
# ---------------------------------------------------------------------------


def test_numeric_noise_below_1e6_does_not_break_idempotency():
    """A last-digit AVG difference below 1e-6 must not cause a re-diagnosis.

    D11 fix (fixed number formatting): if two consecutive Athena runs return
    slightly different float values (e.g. 1.7000001 vs 1.7000002 in a distributed
    AVG), the inputs_hash must be identical so the second run is a no-op.

    This test verifies the idempotency property: feed two evidence dicts that
    differ only below 1e-6 into compute_inputs_hash and assert they produce
    the same hash.
    """
    from agents.fi_cost.evidence import compute_inputs_hash

    e1 = dict(_BASE_EVIDENCE, **{"ratio_cohort": 1.7000001000})
    e2 = dict(_BASE_EVIDENCE, **{"ratio_cohort": 1.7000001999})
    assert compute_inputs_hash(e1) == compute_inputs_hash(e2), (
        "Sub-1e-6 float noise must not change inputs_hash (D11). "
        "Without fixed number formatting, a re-run would treat the same data "
        "as changed and make a spurious model call."
    )
