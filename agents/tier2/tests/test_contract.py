"""
Red-phase contract tests — pins spec § D4 (M4 artifact schema).

These tests FAIL on ModuleNotFoundError until agents/tier2/contract.py is written.
No try/except or importorskip around the imports — failure is the point of the red phase.

Interface pinned:
    from agents.tier2.contract import (
        validate_artifact,       # (dict) -> dict  raises ValueError on invalid input
        build_pk,                # (vin: str) -> str
        build_sk,                # (component: str, inputs_hash: str) -> str
        VALID_COMPONENTS,        # frozenset — {"tires", "battery"} in S1
        VALID_CONFIDENCES,       # frozenset — {"high", "medium", "low"}
        VALID_PROVENANCES,       # frozenset — {"live", "mocked", "absent"}
    )

All tests are synchronous (pytest-asyncio is absent from this venv).
"""

import hashlib
from datetime import datetime, timezone

from agents.tier2.contract import (  # noqa: E402  — intentional: fails until T2.1
    VALID_COMPONENTS,
    VALID_CONFIDENCES,
    VALID_PROVENANCES,
    build_pk,
    build_sk,
    validate_artifact,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_COMPUTED_AT = "2026-08-03T15:00:00Z"
SAMPLE_VIN = "1HGCM82633A004352"


def _minimal_artifact(
    vin: str = SAMPLE_VIN,
    component: str = "battery",
) -> dict:
    """Return the smallest dict that should pass validate_artifact()."""
    computed_at = SAMPLE_COMPUTED_AT
    pk = f"VEH#{vin}"
    inputs_hash = hashlib.sha256(b"test-input-data").hexdigest()
    # sk is content-addressed: FINDING#{component}#{inputs_hash} — spec § D4 (corrected 2026-08-04)
    sk = f"FINDING#{component}#{inputs_hash}"
    return {
        "pk": pk,
        "sk": sk,
        "computed_at": computed_at,
        "confidence": "high",
        "evidence": [
            {
                "source": "energy_usage",
                "value": {"state_of_health_pct": 92.0},
                "provenance": "live",
            }
        ],
        "agent_version": "tier2-health-v0.1.0",
        "inputs_hash": inputs_hash,
        "finding": "Battery is at 92%, degrading normally for its age.",
        "severity_hint": "routine",
        "ttl": 1722700800,  # epoch seconds
    }


# ---------------------------------------------------------------------------
# VALID_COMPONENTS
# ---------------------------------------------------------------------------


def test_valid_components_contains_tires():
    """component='tires' must be recognised — tire_health is now deployed in ADP (unblocked 2026-08-04)."""
    assert "tires" in VALID_COMPONENTS


def test_valid_components_contains_battery():
    assert "battery" in VALID_COMPONENTS


def test_valid_components_contains_brakes():
    """component='brakes' must be recognised — service_records is deployed in ADP."""
    assert "brakes" in VALID_COMPONENTS


def test_valid_components_is_exactly_the_four_registered():
    """Three ADP components plus ``diagnostics`` (active CMS DTCs; spec
    2026-09-25-avx-own-vehicle-findings T4.2). Unknown components must be rejected."""
    assert VALID_COMPONENTS == frozenset({"tires", "battery", "brakes", "diagnostics"})


# ---------------------------------------------------------------------------
# VALID_CONFIDENCES
# ---------------------------------------------------------------------------


def test_valid_confidences_three_values():
    assert VALID_CONFIDENCES == frozenset({"high", "medium", "low"})


# ---------------------------------------------------------------------------
# VALID_PROVENANCES
# ---------------------------------------------------------------------------


def test_valid_provenances_three_values():
    """S1 has no mocked input, but the field is introduced here so S6/M14 doesn't retrofit it."""
    assert VALID_PROVENANCES == frozenset({"live", "mocked", "absent"})


# ---------------------------------------------------------------------------
# build_pk
# ---------------------------------------------------------------------------


def test_build_pk_format():
    pk = build_pk("1HGCM82633A004352")
    assert pk == "VEH#1HGCM82633A004352"


def test_build_pk_prefix():
    pk = build_pk("TESTVIN123")
    assert pk.startswith("VEH#")


# ---------------------------------------------------------------------------
# build_sk
# ---------------------------------------------------------------------------

SAMPLE_INPUTS_HASH = hashlib.sha256(b"test-build-sk").hexdigest()


def test_build_sk_format_battery():
    sk = build_sk("battery", SAMPLE_INPUTS_HASH)
    assert sk == f"FINDING#battery#{SAMPLE_INPUTS_HASH}"


def test_build_sk_format_tires():
    sk = build_sk("tires", SAMPLE_INPUTS_HASH)
    assert sk == f"FINDING#tires#{SAMPLE_INPUTS_HASH}"


def test_build_sk_format_brakes():
    sk = build_sk("brakes", SAMPLE_INPUTS_HASH)
    assert sk == f"FINDING#brakes#{SAMPLE_INPUTS_HASH}"


def test_build_sk_prefix():
    sk = build_sk("battery", SAMPLE_INPUTS_HASH)
    assert sk.startswith("FINDING#")


def test_build_sk_invalid_component_raises():
    """build_sk must reject components not in VALID_COMPONENTS."""
    try:
        build_sk("engine", SAMPLE_INPUTS_HASH)
        assert False, "Expected ValueError for unknown component"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# validate_artifact — required fields present
# ---------------------------------------------------------------------------


def test_validate_artifact_returns_dict():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result, dict)


def test_validate_artifact_all_required_fields_present():
    """All M4 fields must be present after validation."""
    result = validate_artifact(_minimal_artifact())
    required = {
        "pk",
        "sk",
        "computed_at",
        "confidence",
        "evidence",
        "agent_version",
        "inputs_hash",
        "finding",
        "severity_hint",
        "ttl",
    }
    missing = required - set(result.keys())
    assert not missing, f"Missing fields after validate_artifact: {missing}"


# ---------------------------------------------------------------------------
# validate_artifact — pk / sk shape
# ---------------------------------------------------------------------------


def test_validate_artifact_pk_shape():
    result = validate_artifact(_minimal_artifact(vin=SAMPLE_VIN))
    assert result["pk"] == f"VEH#{SAMPLE_VIN}"


def test_validate_artifact_sk_contains_component():
    result = validate_artifact(_minimal_artifact(component="battery"))
    assert "battery" in result["sk"]


def test_validate_artifact_sk_contains_inputs_hash():
    """sk is content-addressed: it must contain the inputs_hash, not computed_at.

    This is the corrected design from spec § D4 (2026-08-04): using computed_at
    in the sort key made M6 unachievable because tomorrow's run over unchanged
    data would target a different item and write a duplicate.
    """
    artifact = _minimal_artifact()
    result = validate_artifact(artifact)
    assert artifact["inputs_hash"] in result["sk"], (
        "sk must embed inputs_hash (content-addressed), not computed_at. "
        "Spec § D4: FINDING#{component}#{inputs_hash}"
    )


def test_validate_artifact_sk_does_not_contain_computed_at():
    """computed_at must NOT appear in the sort key — it is an attribute only.

    The old design (sk = FINDING#{component}#{computed_at}) made M6 unachievable.
    Spec § D4 (corrected 2026-08-04): the sk is content-addressed so idempotency
    is structural.  computed_at stays as an attribute for M5 ("as of last Tuesday").
    """
    artifact = _minimal_artifact()
    result = validate_artifact(artifact)
    assert artifact["computed_at"] not in result["sk"], (
        "computed_at must not appear in the sk — the sort key is "
        "content-addressed (inputs_hash), not timestamp-based."
    )


def test_validate_artifact_sk_starts_with_finding():
    result = validate_artifact(_minimal_artifact())
    assert result["sk"].startswith("FINDING#")


def test_validate_artifact_component_tires_sk():
    artifact = _minimal_artifact(component="tires")
    inputs_hash = artifact["inputs_hash"]
    artifact["sk"] = f"FINDING#tires#{inputs_hash}"
    result = validate_artifact(artifact)
    assert "tires" in result["sk"]


def test_validate_artifact_component_brakes_sk():
    artifact = _minimal_artifact(component="brakes")
    inputs_hash = artifact["inputs_hash"]
    artifact["sk"] = f"FINDING#brakes#{inputs_hash}"
    result = validate_artifact(artifact)
    assert "brakes" in result["sk"]


# ---------------------------------------------------------------------------
# validate_artifact — invalid pk / sk rejected
# ---------------------------------------------------------------------------


def test_validate_artifact_bad_pk_raises():
    artifact = _minimal_artifact()
    artifact["pk"] = "BADPREFIX#123"
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError for bad pk"
    except ValueError:
        pass


def test_validate_artifact_bad_component_in_sk_raises():
    artifact = _minimal_artifact()
    # Use a valid hash format but an invalid component — "engine" is not in VALID_COMPONENTS
    artifact["sk"] = f"FINDING#engine#{artifact['inputs_hash']}"
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError for unknown component in sk"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# validate_artifact — confidence
# ---------------------------------------------------------------------------


def test_validate_artifact_valid_confidence_high():
    artifact = _minimal_artifact()
    artifact["confidence"] = "high"
    result = validate_artifact(artifact)
    assert result["confidence"] == "high"


def test_validate_artifact_valid_confidence_medium():
    artifact = _minimal_artifact()
    artifact["confidence"] = "medium"
    result = validate_artifact(artifact)
    assert result["confidence"] == "medium"


def test_validate_artifact_valid_confidence_low():
    artifact = _minimal_artifact()
    artifact["confidence"] = "low"
    result = validate_artifact(artifact)
    assert result["confidence"] == "low"


def test_validate_artifact_invalid_confidence_raises():
    artifact = _minimal_artifact()
    artifact["confidence"] = "unknown"
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError for invalid confidence"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# validate_artifact — evidence[] entries and provenance
# ---------------------------------------------------------------------------


def test_validate_artifact_evidence_is_list():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["evidence"], list)


def test_validate_artifact_evidence_entry_has_source():
    result = validate_artifact(_minimal_artifact())
    assert "source" in result["evidence"][0]


def test_validate_artifact_evidence_entry_has_value():
    result = validate_artifact(_minimal_artifact())
    assert "value" in result["evidence"][0]


def test_validate_artifact_evidence_entry_has_provenance():
    result = validate_artifact(_minimal_artifact())
    assert "provenance" in result["evidence"][0]


def test_validate_artifact_evidence_provenance_live():
    artifact = _minimal_artifact()
    artifact["evidence"][0]["provenance"] = "live"
    result = validate_artifact(artifact)
    assert result["evidence"][0]["provenance"] == "live"


def test_validate_artifact_evidence_provenance_mocked():
    """S1 has no mocked inputs, but the schema must support mocked for future specs."""
    artifact = _minimal_artifact()
    artifact["evidence"][0]["provenance"] = "mocked"
    result = validate_artifact(artifact)
    assert result["evidence"][0]["provenance"] == "mocked"


def test_validate_artifact_evidence_provenance_absent():
    artifact = _minimal_artifact()
    artifact["evidence"][0]["provenance"] = "absent"
    result = validate_artifact(artifact)
    assert result["evidence"][0]["provenance"] == "absent"


def test_validate_artifact_evidence_invalid_provenance_raises():
    artifact = _minimal_artifact()
    artifact["evidence"][0]["provenance"] = "fake"
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError for invalid provenance"
    except ValueError:
        pass


def test_validate_artifact_evidence_empty_list_allowed():
    """An artifact with no evidence entries is valid (e.g., absent-data scenario)."""
    artifact = _minimal_artifact()
    artifact["evidence"] = []
    result = validate_artifact(artifact)
    assert result["evidence"] == []


# ---------------------------------------------------------------------------
# validate_artifact — inputs_hash
# ---------------------------------------------------------------------------


def test_validate_artifact_inputs_hash_is_string():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["inputs_hash"], str)


def test_validate_artifact_inputs_hash_looks_like_sha256():
    """inputs_hash is SHA-256: 64 hex chars."""
    result = validate_artifact(_minimal_artifact())
    assert len(result["inputs_hash"]) == 64
    assert all(c in "0123456789abcdef" for c in result["inputs_hash"].lower())


def test_validate_artifact_missing_inputs_hash_raises():
    artifact = _minimal_artifact()
    del artifact["inputs_hash"]
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError for missing inputs_hash"
    except (ValueError, KeyError):
        pass


# ---------------------------------------------------------------------------
# validate_artifact — ttl
# ---------------------------------------------------------------------------


def test_validate_artifact_ttl_is_integer():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["ttl"], int)


def test_validate_artifact_ttl_positive():
    result = validate_artifact(_minimal_artifact())
    assert result["ttl"] > 0


# ---------------------------------------------------------------------------
# validate_artifact — computed_at
# ---------------------------------------------------------------------------


def test_validate_artifact_computed_at_is_string():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["computed_at"], str)


def test_validate_artifact_computed_at_non_empty():
    result = validate_artifact(_minimal_artifact())
    assert result["computed_at"]


def test_validate_artifact_missing_computed_at_raises():
    artifact = _minimal_artifact()
    del artifact["computed_at"]
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError for missing computed_at"
    except (ValueError, KeyError):
        pass


# ---------------------------------------------------------------------------
# validate_artifact — finding and severity_hint
# ---------------------------------------------------------------------------


def test_validate_artifact_finding_is_string():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["finding"], str)


def test_validate_artifact_finding_non_empty():
    result = validate_artifact(_minimal_artifact())
    assert result["finding"].strip()


def test_validate_artifact_severity_hint_is_string():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["severity_hint"], str)


def test_validate_artifact_severity_hint_not_p_level():
    """severity_hint is advisory only — must not carry P0/P1/P2/P3 vocabulary."""
    result = validate_artifact(_minimal_artifact())
    # The hint should not equal a triage P-level
    assert result["severity_hint"] not in {"P0", "P1", "P2", "P3"}


def test_validate_artifact_severity_hint_p_levels_rejected():
    """Explicitly test that P-level strings are rejected by validate_artifact."""
    for p_level in ("P0", "P1", "P2", "P3"):
        artifact = _minimal_artifact()
        artifact["severity_hint"] = p_level
        try:
            validate_artifact(artifact)
            assert False, f"Expected ValueError for severity_hint='{p_level}'"
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# validate_artifact — agent_version
# ---------------------------------------------------------------------------


def test_validate_artifact_agent_version_is_string():
    result = validate_artifact(_minimal_artifact())
    assert isinstance(result["agent_version"], str)


def test_validate_artifact_agent_version_non_empty():
    result = validate_artifact(_minimal_artifact())
    assert result["agent_version"].strip()


# ---------------------------------------------------------------------------
# validate_artifact — missing required fields raise
# ---------------------------------------------------------------------------


def test_validate_artifact_missing_pk_raises():
    artifact = _minimal_artifact()
    del artifact["pk"]
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError/KeyError for missing pk"
    except (ValueError, KeyError):
        pass


def test_validate_artifact_missing_sk_raises():
    artifact = _minimal_artifact()
    del artifact["sk"]
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError/KeyError for missing sk"
    except (ValueError, KeyError):
        pass


def test_validate_artifact_missing_finding_raises():
    artifact = _minimal_artifact()
    del artifact["finding"]
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError/KeyError for missing finding"
    except (ValueError, KeyError):
        pass


def test_validate_artifact_missing_severity_hint_raises():
    artifact = _minimal_artifact()
    del artifact["severity_hint"]
    try:
        validate_artifact(artifact)
        assert False, "Expected ValueError/KeyError for missing severity_hint"
    except (ValueError, KeyError):
        pass
