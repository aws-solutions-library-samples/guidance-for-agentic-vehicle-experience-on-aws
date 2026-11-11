"""
Tests for agents/tier2/provenance_guard.py — mock-provenance guard.

Spec reference: specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md Group 2,
task "Author mock-provenance guard (Spec 1b inheritance)".

Design principles (recorded in decisions.md Fix Groups 8 and 9 as hard-won lessons):

  1. No prose reading — boundaries are exact set membership, not substring/regex checks.
     Every expectation is a table compared against structured data.

  2. Mutation values come from outside the assertion's own vocabulary.
     The guard's triggering value is provenance="mocked"; mutation (b) uses "absent"
     (a valid VALID_PROVENANCES value that is NOT in the guard's trigger set {"mocked"}).
     A mutation drawn from {"mocked"} would only prove the denylist contains it.

  3. "Covered by construction" is a claim, not a measurement.
     Each property is asserted independently; nothing is assumed unreachable.

  4. Mutations are run against cp copies; NEVER restored via git checkout.

Test taxonomy:

  Baseline
    test_guard_fires_on_mocked_monetary_finding
    test_guard_does_not_fire_on_live_monetary_finding
    test_guard_does_not_fire_on_mocked_non_monetary_finding
    test_guard_does_not_fire_empty_evidence

  Mode behaviour
    test_watermark_mode_attaches_disclosure_txt_when_absent
    test_watermark_mode_extends_existing_disclosure_txt
    test_refuse_mode_raises_provenance_guard_error
    test_refuse_mode_does_not_fire_on_live_provenance
    test_invalid_mode_raises_value_error

  Detection helpers (each tested independently — "covered by construction" is a claim)
    test_has_mocked_provenance_true_on_single_mocked_entry
    test_has_mocked_provenance_false_all_live
    test_has_mocked_provenance_false_empty_evidence
    test_has_mocked_provenance_case_sensitive
    test_is_monetary_finding_true_by_kind
    test_is_monetary_finding_true_by_disclosure_txt
    test_is_monetary_finding_false_health_kind_no_disclosure

  Exact vocabulary tables (expectation tables over structured data — no prose)
    test_monetary_kinds_table_matches_schema_enum
    test_watermark_constant_is_non_empty_string

  Mutation-verified contracts (documented in decisions-fragment-provenance-guard.md)
    These test names are the mutation targets. Mutating the guard call from
    render_finding() OR mutating provenance="mocked" to "absent" in the fixture
    MUST make exactly the named test fail.
    test_render_calls_guard_on_monetary_mocked_finding  — mutation (a) target
    test_fixture_mocked_entry_triggers_guard             — mutation (b) target
"""

import pytest

from agents.tier2.provenance_guard import (
    ILLUSTRATIVE_WATERMARK,
    _MONETARY_FINDING_KINDS,
    _has_mocked_provenance,
    _is_monetary_finding,
    ProvenanceGuardError,
    render_finding,
)

# ---------------------------------------------------------------------------
# Exact vocabulary table — the test's independent expectation of monetary kinds.
# This table is compared against _MONETARY_FINDING_KINDS structurally, not by
# reading prose documentation.
# ---------------------------------------------------------------------------

_EXPECTED_MONETARY_KINDS: frozenset = frozenset({
    "coverage.warranty",
    "coverage.recall",
    "energy.cost",
    "loyalty.points",
    "loyalty.expiry",
})
"""Test-side expectation table for monetary finding_kind values.
Sourced from spec.md § 'Cross-surface Action data model — canonical' allOf[1].if enum.
Any addition to _MONETARY_FINDING_KINDS requires a deliberate edit to this table."""


# ---------------------------------------------------------------------------
# Synthetic test fixtures (no real Agent 2 fixtures — Agent 2 has no mocked seams).
# The fixture uses finding_kind="coverage.warranty" which is in _MONETARY_FINDING_KINDS.
# The single mocked evidence entry has provenance="mocked".
#
# Mutation (b) target: provenance="mocked" → "absent"
# "absent" is drawn from VALID_PROVENANCES but NOT from the guard's trigger set {"mocked"}.
# ---------------------------------------------------------------------------

def _make_monetary_finding_with_mocked_provenance() -> dict:
    """Return a synthetic monetary Finding with one mocked-provenance evidence entry.

    This fixture does NOT come from the schemas/fixtures directory — it is test-only
    and crafted to exercise the guard. Agent 2 has no mocked seams in production; this
    fixture exists only so the guard can be unit-tested in Spec 1a.

    The finding_kind is "coverage.warranty" — a Spec 1b Agent 3 kind that is in
    _MONETARY_FINDING_KINDS. The guard is designed to be agent-agnostic.
    """
    return {
        "finding_id": "FIND-aaaabbbb-cccc-dddd-eeee-ffffffffffff",
        "finding_key": "a" * 64,
        "agent_id": "agent-3-ownership-cost",
        "agent_version": "0.1.0",
        "scope": {"vin": "1TESTVIN0TEST00001"},
        "severity": "soon",
        "computed_at": "2026-09-18T03:00:00Z",
        "expires_at": "2026-09-25T03:00:00Z",
        "confidence": "medium",
        "finding_kind": "coverage.warranty",
        "headline": "Warranty coverage may have lapsed on powertrain",
        "detail": "Estimated powertrain warranty coverage status based on mileage.",
        # disclosure_txt is absent here — the guard should add ILLUSTRATIVE_WATERMARK
        "evidence": [
            {
                "kind": "dms.warranty.coverage_check",
                "source": "dms-staging",
                "source_ref": "mocked-stub-warranty-001",
                "value": {
                    "covered": None,
                    "reason": "DMS warranty seam mocked for Spec 1b Agent 3 inheritance",
                    "estimated_remaining_usd": 1200.00,
                },
                "computed_at": "2026-09-18T03:00:00Z",
                "provenance": "mocked",  # <-- MUTATION (b) TARGET: change to "absent"
            },
        ],
        "inputs_hash": "b" * 64,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
    }


def _make_monetary_finding_with_live_provenance() -> dict:
    """Return a monetary Finding where all evidence is live — guard must NOT fire."""
    finding = _make_monetary_finding_with_mocked_provenance()
    result = dict(finding)
    result["evidence"] = [
        dict(entry, provenance="live")
        for entry in finding["evidence"]
    ]
    return result


def _make_health_finding_with_mocked_provenance() -> dict:
    """Return a non-monetary Finding (health.brakes) with mocked evidence — guard must NOT fire."""
    return {
        "finding_id": "FIND-11112222-3333-4444-5555-666677778888",
        "finding_key": "c" * 64,
        "agent_id": "agent-2-vehicle-health",
        "agent_version": "1.0.0",
        "scope": {"vin": "2TESTVIN0TEST00002"},
        "severity": "soon",
        "computed_at": "2026-09-18T02:20:00Z",
        "expires_at": "2026-09-25T02:20:00Z",
        "confidence": "medium",
        "finding_kind": "health.brakes",
        "headline": "Brake pad wear at 35%",
        "detail": "Front brake pads at 35% wear.",
        "evidence": [
            {
                "kind": "dms.warranty.brake_coverage",
                "source": "dms-staging",
                "source_ref": "mocked-stub-brakes-001",
                "value": {"covered": None, "reason": "mocked"},
                "computed_at": "2026-09-18T02:20:00Z",
                "provenance": "mocked",
            },
        ],
        "inputs_hash": "d" * 64,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
    }


# ---------------------------------------------------------------------------
# Exact vocabulary table tests
# ---------------------------------------------------------------------------


def test_monetary_kinds_table_matches_schema_enum():
    """_MONETARY_FINDING_KINDS must equal _EXPECTED_MONETARY_KINDS exactly.

    This test uses exact set equality — not subset, not superset. Adding a kind to the
    implementation without updating this table fails here; adding a kind to this table
    without updating the implementation also fails. Deliberate review required for either.
    """
    assert _MONETARY_FINDING_KINDS == _EXPECTED_MONETARY_KINDS, (
        f"Mismatch between implementation and expected table.\n"
        f"  Unexpected in implementation: {_MONETARY_FINDING_KINDS - _EXPECTED_MONETARY_KINDS}\n"
        f"  Missing from implementation:  {_EXPECTED_MONETARY_KINDS - _MONETARY_FINDING_KINDS}"
    )


def test_watermark_constant_is_non_empty_string():
    """ILLUSTRATIVE_WATERMARK must be a non-empty string — it is the text surfaced to users."""
    assert isinstance(ILLUSTRATIVE_WATERMARK, str)
    assert len(ILLUSTRATIVE_WATERMARK.strip()) > 0


# ---------------------------------------------------------------------------
# Detection helper tests — each property measured independently
# ---------------------------------------------------------------------------


def test_has_mocked_provenance_true_on_single_mocked_entry():
    finding = _make_monetary_finding_with_mocked_provenance()
    assert _has_mocked_provenance(finding) is True


def test_has_mocked_provenance_false_all_live():
    finding = _make_monetary_finding_with_live_provenance()
    assert _has_mocked_provenance(finding) is False


def test_has_mocked_provenance_false_empty_evidence():
    finding = dict(_make_monetary_finding_with_mocked_provenance(), evidence=[])
    assert _has_mocked_provenance(finding) is False


def test_has_mocked_provenance_case_sensitive():
    """provenance='Mocked' must NOT trigger the guard — exact equality only."""
    # "Mocked" is NOT in the guard's trigger set {"mocked"}.
    # This is drawn from outside the guard's vocabulary (different case = different string).
    finding = dict(
        _make_monetary_finding_with_mocked_provenance(),
        evidence=[
            {
                "kind": "test.kind",
                "source": "test",
                "source_ref": "test-001",
                "value": {},
                "computed_at": "2026-09-18T03:00:00Z",
                "provenance": "Mocked",  # capitalised — should NOT match
            }
        ],
    )
    assert _has_mocked_provenance(finding) is False


def test_is_monetary_finding_true_by_kind():
    """A finding_kind in _MONETARY_FINDING_KINDS must be detected as monetary."""
    finding = _make_monetary_finding_with_mocked_provenance()
    assert _is_monetary_finding(finding) is True


def test_is_monetary_finding_true_by_disclosure_txt():
    """A finding with non-empty disclosure_txt must be detected as monetary even if
    finding_kind is not in _MONETARY_FINDING_KINDS (forward-compatibility path)."""
    finding = {
        "finding_kind": "health.brakes",  # NOT in _MONETARY_FINDING_KINDS
        "disclosure_txt": "Estimated repair cost: $350.",
    }
    assert _is_monetary_finding(finding) is True


def test_is_monetary_finding_false_health_kind_no_disclosure():
    """health.brakes without disclosure_txt is not monetary."""
    finding = _make_health_finding_with_mocked_provenance()
    assert _is_monetary_finding(finding) is False


# ---------------------------------------------------------------------------
# Baseline guard behaviour
# ---------------------------------------------------------------------------


def test_guard_fires_on_mocked_monetary_finding():
    """Watermark is added when provenance=mocked AND finding is monetary."""
    finding = _make_monetary_finding_with_mocked_provenance()
    result = render_finding(finding)
    assert "disclosure_txt" in result
    assert ILLUSTRATIVE_WATERMARK in result["disclosure_txt"]


def test_guard_does_not_fire_on_live_monetary_finding():
    """No watermark when all evidence is live, even for a monetary finding."""
    finding = _make_monetary_finding_with_live_provenance()
    result = render_finding(finding)
    # Guard must not fire — disclosure_txt should not be added
    assert result.get("disclosure_txt", "") == "", (
        "Guard fired incorrectly on a finding with live-only provenance"
    )


def test_guard_does_not_fire_on_mocked_non_monetary_finding():
    """No watermark when provenance=mocked but finding is NOT monetary."""
    finding = _make_health_finding_with_mocked_provenance()
    result = render_finding(finding)
    assert "disclosure_txt" not in result


def test_guard_does_not_fire_empty_evidence():
    """No watermark when evidence list is empty."""
    finding = dict(_make_monetary_finding_with_mocked_provenance(), evidence=[])
    result = render_finding(finding)
    assert result.get("disclosure_txt", "") == ""


def test_render_does_not_mutate_caller_dict():
    """render_finding must not modify the caller's original dict in watermark mode."""
    finding = _make_monetary_finding_with_mocked_provenance()
    original_keys = set(finding.keys())
    _ = render_finding(finding)
    assert set(finding.keys()) == original_keys
    assert "disclosure_txt" not in finding


# ---------------------------------------------------------------------------
# Mode behaviour
# ---------------------------------------------------------------------------


def test_watermark_mode_attaches_disclosure_txt_when_absent():
    """In watermark mode, disclosure_txt is added and contains ILLUSTRATIVE_WATERMARK."""
    finding = _make_monetary_finding_with_mocked_provenance()
    assert "disclosure_txt" not in finding  # confirm fixture is correct
    result = render_finding(finding, mode="watermark")
    assert result["disclosure_txt"] == ILLUSTRATIVE_WATERMARK


def test_watermark_mode_extends_existing_disclosure_txt():
    """In watermark mode, an existing disclosure_txt is extended, not replaced."""
    original_disclosure = "Warranty coverage data from DMS partner API."
    finding = dict(
        _make_monetary_finding_with_mocked_provenance(),
        disclosure_txt=original_disclosure,
    )
    result = render_finding(finding, mode="watermark")
    assert original_disclosure in result["disclosure_txt"]
    assert ILLUSTRATIVE_WATERMARK in result["disclosure_txt"]
    # Both must be present — not truncated, not replaced
    assert result["disclosure_txt"].startswith(original_disclosure.rstrip())


def test_refuse_mode_raises_provenance_guard_error():
    """In refuse mode, ProvenanceGuardError (subclass of ValueError) is raised."""
    finding = _make_monetary_finding_with_mocked_provenance()
    with pytest.raises(ProvenanceGuardError):
        render_finding(finding, mode="refuse")


def test_refuse_mode_error_is_subclass_of_value_error():
    """ProvenanceGuardError is a ValueError — callers catching ValueError get it."""
    finding = _make_monetary_finding_with_mocked_provenance()
    with pytest.raises(ValueError):
        render_finding(finding, mode="refuse")


def test_refuse_mode_does_not_fire_on_live_provenance():
    """refuse mode does NOT raise when all provenance is live."""
    finding = _make_monetary_finding_with_live_provenance()
    result = render_finding(finding, mode="refuse")  # must not raise
    assert result is finding or result == finding  # unchanged


def test_invalid_mode_raises_value_error():
    """An unknown mode raises ValueError, not ProvenanceGuardError."""
    finding = _make_monetary_finding_with_mocked_provenance()
    with pytest.raises(ValueError, match="mode"):
        render_finding(finding, mode="invalid-mode")


# ---------------------------------------------------------------------------
# Mutation-verified contracts (named tests are the mutation targets)
#
# Mutation (a): drop the guard call from the render path (comment out the
#   _has_mocked_provenance + _is_monetary_finding check in render_finding).
#   → test_render_calls_guard_on_monetary_mocked_finding MUST fail.
#
# Mutation (b): in _make_monetary_finding_with_mocked_provenance(), change
#   provenance="mocked" to provenance="absent".
#   "absent" is drawn from VALID_PROVENANCES but NOT from the guard's trigger
#   set {"mocked"} — it is outside the assertion's own vocabulary.
#   → test_fixture_mocked_entry_triggers_guard MUST fail.
# ---------------------------------------------------------------------------


def test_render_calls_guard_on_monetary_mocked_finding():
    """render_finding MUST invoke the guard (checking mocked provenance + monetary kind)
    and watermark the result. Mutation (a) — commenting out the guard call — makes this
    test fail because disclosure_txt is absent from the returned dict.

    Assertion: result must contain 'disclosure_txt' equal to ILLUSTRATIVE_WATERMARK.
    This is a structural check on the returned dict, not a side-effect spy.
    """
    finding = _make_monetary_finding_with_mocked_provenance()
    result = render_finding(finding)
    # This assertion fails under mutation (a): result won't have disclosure_txt
    assert "disclosure_txt" in result, (
        "render_finding must add disclosure_txt when mocked provenance + monetary finding; "
        "if this fails after mutation (a), the guard call was dropped"
    )
    assert result["disclosure_txt"] == ILLUSTRATIVE_WATERMARK, (
        f"Expected ILLUSTRATIVE_WATERMARK; got {result.get('disclosure_txt')!r}"
    )


def test_fixture_mocked_entry_triggers_guard():
    """The fixture's single evidence entry MUST have provenance='mocked' for the guard
    to fire. Mutation (b) — changing that entry's provenance to 'absent' — makes this
    test fail because the guard no longer fires and disclosure_txt is absent.

    'absent' is drawn from VALID_PROVENANCES (a sibling provenance value) but is NOT
    in the guard's trigger set {'mocked'} — it is outside the assertion's own vocabulary,
    satisfying the mutation-value discipline from decisions.md Fix Groups 8/9.

    Assertion: the fixture's evidence list contains exactly one entry with
    provenance='mocked'. This is an exact count + value check on structured data.
    """
    finding = _make_monetary_finding_with_mocked_provenance()
    mocked_entries = [
        e for e in finding["evidence"] if e.get("provenance") == "mocked"
    ]
    # Under mutation (b), provenance is "absent", so mocked_entries is empty:
    assert len(mocked_entries) == 1, (
        f"Expected exactly 1 evidence entry with provenance='mocked'; "
        f"found {len(mocked_entries)}. "
        f"If this fails after mutation (b), the fixture's mocked tag was removed."
    )
    # Confirm the guard fires (render produces disclosure_txt) — end-to-end check.
    # Under mutation (b) this also fails because _has_mocked_provenance returns False.
    result = render_finding(finding)
    assert "disclosure_txt" in result, (
        "Guard did not fire — provenance='mocked' entry must be present in the fixture "
        "for test_fixture_mocked_entry_triggers_guard to pass."
    )
