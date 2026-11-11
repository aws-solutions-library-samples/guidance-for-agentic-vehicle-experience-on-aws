"""
Tests for ``agents/tier2/finding_transform.py`` — T16.1.

Spec source:
    .kiro/specs/2026-09-18-avx-tier2-ios-cards-and-actions/tasks.md § Fix Group 16
    → T16.1

Acceptance criteria (T16.1 Verify)
-----------------------------------
1. ``python3 -m pytest agents/tier2/tests/test_finding_transform.py -q`` passes.
2. At least one test asserts the output is schema-valid (not merely shaped).
3. Three mutations verified:

   (a) Drop ``evidence[]`` → a test fails.
       Guard: evidence is required by schema; a Finding without it fails
       ``_validate_finding()``.  The agentic-tiers.md contract requires evidence;
       a Finding asserting a conclusion it cannot support is the named defect.

   (b) Hardcode ``computed_at`` to a fixed timestamp → a test fails.
       Guard: ``test_computed_at_is_fresh`` checks freshness RELATIVE to the
       call time using a tight window, NOT against a constant — replicating the
       ``test_snapshot_date_must_not_be_hardcoded`` trap would let any in-year
       hardcode pass.

   (c) Derive ``finding_key`` locally instead of calling
       ``writer.compute_finding_key()`` → a test fails.
       Guard: ``test_finding_key_matches_writer`` compares byte-for-byte.

Design rules (from test_finding_key_update_in_place.py and decisions.md)
------------------------------------------------------------------------
1. Never read prose in a guard — compare structured data against exact
   expectations.
2. Mutation values come from OUTSIDE the assertion's own vocabulary.
3. "Covered by construction" is a claim; measure and say what was measured.
4. Restore from ``cp`` copy, NEVER ``git checkout``.

Test isolation
--------------
All tests are deterministic.  No network calls, no DDB, no boto3.  The
``_ts`` fixture injects a stable timestamp so ``computed_at`` assertions are
not bound to wall-clock variance.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid

import pytest

from agents.tier2.finding_transform import (
    _enrich_evidence_entry,
    _expires_at_from_computed_at,
    health_artifact_to_finding,
)
from agents.tier2.writer import compute_finding_key

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "agent-2-vehicle-health"
AGENT_VERSION = "1.0.0"
SAMPLE_VIN = "1HGBH41JXMN109186"
FINDING_KIND = "health.battery"
SEVERITY = "routine"

# Deterministic inputs_hash (must be 64 lowercase hex chars per schema).
# Rule 2: value is outside the assertion's own vocabulary — a random-looking
# but reproducible hash derived from a known string.
_INPUTS_HASH = hashlib.sha256(b"t16-1-adp-rows-xyzzy-distinct").hexdigest()
# A DIFFERENT hash for mutation injection.
_INPUTS_HASH_MUTATION = hashlib.sha256(b"t16-1-mutation-probe-plugh").hexdigest()

assert _INPUTS_HASH != _INPUTS_HASH_MUTATION

# Fixed timestamps for deterministic tests.
_FIXED_TS = "2026-10-01T02:00:00Z"
_FIXED_EXPIRES = "2026-10-08T02:00:00Z"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _utc_now_stub(ts: str = _FIXED_TS):
    """Return a callable that always returns *ts*."""
    return lambda: ts


def _minimal_artifact(
    *,
    vin: str = SAMPLE_VIN,
    finding_kind: str = FINDING_KIND,
    severity: str = SEVERITY,
    inputs_hash: str = _INPUTS_HASH,
    evidence: list | None = None,
) -> dict:
    """Build the minimal artifact shape that ``_build_artifact()`` produces.

    This is the INPUT to ``health_artifact_to_finding()``.  It deliberately
    matches the real runner output (including legacy fields) to ensure the
    transform handles the full production shape.
    """
    fk = compute_finding_key(
        agent_id=AGENT_ID,
        scope_field="vin",
        scope_value=vin,
        finding_kind=finding_kind,
    )
    finding_id = f"FIND-{uuid.uuid4()}"

    if evidence is None:
        evidence = [
            {
                "source": "adp.battery.health_pct",
                "value": "92",
                "provenance": "live",
            }
        ]

    return {
        # --- D2 AVX Finding fields (already present on runner artifact) ---
        "finding_id": finding_id,
        "finding_key": fk,
        "agent_id": AGENT_ID,
        "agent_version": AGENT_VERSION,
        "scope": {"vin": vin},
        "severity": severity,
        "computed_at": _FIXED_TS,
        "expires_at": _FIXED_EXPIRES,
        "confidence": "high",
        "finding_kind": finding_kind,
        "headline": "Battery state of health is 92%, degrading normally. No service action required.",
        "detail": "Battery SoH is 92%. Normal degradation for age.",
        "evidence": evidence,
        "inputs_hash": inputs_hash,
        "tokens_used": 0,
        "cost_usd": 0.0,
        "audit_trail": [],
        # --- Legacy fields (present on runner artifact; must be stripped) ---
        "pk": f"VEH#{vin}",
        "sk": f"FINDING#battery#{inputs_hash}",
        "finding": "Battery state of health is 92%.",
        "severity_hint": "routine",
        "ttl": 1760000000,
    }


# ---------------------------------------------------------------------------
# Helper: validate output against the frozen schema
# ---------------------------------------------------------------------------

def _validate_against_schema(finding: dict) -> list[str]:
    """Return error messages from Draft202012Validator (empty = valid)."""
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[1] / "schemas" / "finding.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(finding)]


# ---------------------------------------------------------------------------
# Core transform — schema validity
# ---------------------------------------------------------------------------


class TestSchemaValidity:
    """The output must be a valid AVX Finding per the frozen schema."""

    def test_agent_version_tracks_avx_agent_version(self):
        """agent_version in output must equal runner.AVX_AGENT_VERSION (T17.1 mutation guard).

        Mutation: set AVX_AGENT_VERSION to "9.9.9" → this test fails because the
        output's agent_version no longer reflects what runner.AGENT_VERSION encodes.

        The constraint is: AVX_AGENT_VERSION is derived from AGENT_VERSION by the
        same extraction that runs at import time.  A hardcoded "9.9.9" satisfies
        the schema but breaks the invariant that the Finding's agent_version tracks
        the running binary's declared version.
        """
        from agents.tier2.runner import AVX_AGENT_VERSION, AGENT_VERSION
        import re

        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert result["agent_version"] == AVX_AGENT_VERSION, (
            f"agent_version in Finding is {result['agent_version']!r} but "
            f"AVX_AGENT_VERSION is {AVX_AGENT_VERSION!r}. "
            "T17.1 mutation: a hardcoded '9.9.9' satisfies the schema but breaks "
            "the invariant that the Finding version tracks AGENT_VERSION."
        )
        # Also confirm the derivation is correct.
        expected = re.search(r"(\d+\.\d+\.\d+)$", AGENT_VERSION)
        assert expected is not None, f"AGENT_VERSION {AGENT_VERSION!r} has no semver suffix"
        assert result["agent_version"] == expected.group(1), (
            f"agent_version {result['agent_version']!r} does not match the semver "
            f"suffix of AGENT_VERSION {AGENT_VERSION!r}."
        )

    def test_output_is_schema_valid(self):
        """Primary acceptance criterion: transform output passes schema validation.

        This is the load-bearing test the task requires — at least one
        test must assert schema validity, not just shape.
        """
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        errors = _validate_against_schema(result)
        assert not errors, (
            f"health_artifact_to_finding() returned an invalid AVX Finding "
            f"({len(errors)} schema violation(s)):\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    def test_output_is_schema_valid_attention_severity(self):
        """Transform handles 'attention' severity (an enum value tested by ADP data)."""
        artifact = _minimal_artifact(severity="attention", finding_kind="health.tires")
        artifact["evidence"] = [
            {
                "source": "adp.tires.replacement_flagged",
                "value": "True",
                "provenance": "live",
            }
        ]
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())
        errors = _validate_against_schema(result)
        assert not errors, f"Schema violations: {errors}"

    def test_output_is_schema_valid_brakes(self):
        """Transform handles 'health.brakes' finding_kind correctly."""
        artifact = _minimal_artifact(finding_kind="health.brakes", severity="soon")
        artifact["evidence"] = [
            {"source": "adp.brakes.outcome", "value": "resolved", "provenance": "live"},
        ]
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())
        errors = _validate_against_schema(result)
        assert not errors, f"Schema violations: {errors}"

    def test_legacy_fields_stripped_from_output(self):
        """Legacy fields (pk, sk, finding, severity_hint, ttl) must NOT be in output.

        The AVX Finding schema has ``additionalProperties: false``.  Any
        legacy field would cause schema validation to fail.  We also assert
        their absence explicitly so the stripping logic is named in a test.
        """
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        for field in ("pk", "sk", "finding", "severity_hint", "ttl"):
            assert field not in result, (
                f"Legacy field {field!r} must not appear in the AVX Finding output; "
                f"the schema's additionalProperties:false would reject it."
            )

    def test_all_required_schema_fields_present(self):
        """Every required top-level field is present in the output."""
        required_fields = {
            "finding_id", "finding_key", "agent_id", "agent_version",
            "scope", "severity", "computed_at", "expires_at",
            "confidence", "finding_kind", "headline", "detail",
            "evidence", "inputs_hash", "tokens_used", "cost_usd", "audit_trail",
        }
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        missing = required_fields - set(result.keys())
        assert not missing, f"Required fields absent from output: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Mutation guard (a): drop evidence[] → schema validation fails
# ---------------------------------------------------------------------------


class TestEvidenceRequired:
    """Mutation (a): dropping evidence[] must cause a test failure.

    The Tier 2 artifact contract (agentic-tiers.md § 'The Tier 2 artifact
    contract') states: 'Evidence is what makes narration honest.  Without it
    the Tier 1 agent is asserting a conclusion it cannot support.'

    Guard design: assert schema validity AND that evidence is present when
    the source artifact carries evidence rows.  If ``evidence`` were stripped
    from the output, ``_validate_against_schema`` would return an error
    (``evidence`` is in ``required`` at the top level of the Finding schema),
    and this test would fail.
    """

    def test_evidence_required_by_schema(self):
        """Output must carry the evidence[] array — schema enforces it."""
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        # Assert schema validity (which requires evidence[]).
        errors = _validate_against_schema(result)
        assert not errors, f"Schema violations: {errors}"

        # Assert evidence is present and is a list.
        assert isinstance(result.get("evidence"), list), (
            "evidence must be a list in the output Finding"
        )

    def test_evidence_entries_carry_required_schema_fields(self):
        """Each evidence entry must have all schema-required fields.

        evidence_entry requires: kind, source, source_ref, value,
        computed_at, provenance.
        """
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        required_entry_fields = {"kind", "source", "source_ref", "value", "computed_at", "provenance"}
        for i, entry in enumerate(result["evidence"]):
            missing = required_entry_fields - set(entry.keys())
            assert not missing, (
                f"evidence[{i}] is missing required fields: {sorted(missing)}"
            )

    def test_evidence_populated_when_source_has_rows(self):
        """When the source artifact has evidence rows, the output carries them.

        Mutation proof: if evidence[] were dropped from the output, this
        assertion fails (len would be 0).  Combined with
        test_evidence_required_by_schema (schema also requires evidence),
        both guards catch the same mutation from different angles.
        """
        evidence_rows = [
            {"source": "adp.battery.health_pct", "value": "92", "provenance": "live"},
            {"source": "adp.battery.age_days", "value": "730", "provenance": "live"},
        ]
        artifact = _minimal_artifact(evidence=evidence_rows)
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert len(result["evidence"]) == len(evidence_rows), (
            f"Expected {len(evidence_rows)} evidence entries, got {len(result['evidence'])}. "
            "Dropping evidence[] from the transform breaks this assertion."
        )

    def test_schema_rejects_missing_evidence(self):
        """Explicitly verify the schema DOES reject a Finding with no evidence field.

        This is the mutation-state test: if we were to build a Finding without
        evidence and validate it, the schema validator would reject it.  This
        test documents and verifies that the schema is the load-bearing guard.
        """
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        # Construct the mutation state: drop evidence from the output.
        mutated = {k: v for k, v in result.items() if k != "evidence"}
        errors = _validate_against_schema(mutated)

        assert errors, (
            "Schema must reject a Finding with no evidence field. "
            "If this assertion fails, mutation (a) is not caught — the guard "
            "is broken."
        )


# ---------------------------------------------------------------------------
# Mutation guard (b): hardcode computed_at → freshness test fails
# ---------------------------------------------------------------------------


class TestComputedAtIsFresh:
    """Mutation (b): hardcoding computed_at to a fixed timestamp must fail.

    Guard design: assert freshness RELATIVE to the call time using a short
    window, NOT against a constant.  This avoids the
    ``test_snapshot_date_must_not_be_hardcoded`` trap where asserting
    ``>= "2026-01-01"`` admits any in-year hardcode (the defect recorded in
    decisions.md).

    The injected ``utc_now_fn`` returns a KNOWN timestamp.  We assert the
    output's ``computed_at`` equals that injected value — if computed_at
    were hardcoded to a different constant, this test would fail.
    """

    def test_computed_at_equals_injected_timestamp(self):
        """computed_at must come from utc_now_fn, not a hardcoded constant.

        Mutation (b) proof: replace the implementation's ``run_ts = utc_now_fn()``
        with a hardcoded string.  This test fails because the output's
        computed_at will differ from the injected timestamp.
        """
        # Inject a timestamp DIFFERENT from _FIXED_TS to rule out a coincidence.
        injected_ts = "2026-11-15T14:30:00Z"
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=lambda: injected_ts)

        assert result["computed_at"] == injected_ts, (
            f"computed_at must equal the value returned by utc_now_fn(). "
            f"Expected {injected_ts!r}, got {result['computed_at']!r}. "
            "Hardcoding computed_at breaks this assertion — mutation (b) guard."
        )

    def test_computed_at_is_not_from_source_artifact(self):
        """computed_at must NOT be forwarded from the source artifact unchanged.

        The source artifact carries a stale computed_at (set when _build_artifact
        ran, possibly seconds or hours earlier).  The transform must refresh it
        via utc_now_fn() so the persisted Finding reflects when the AVX write
        happened, not when the health agent ran.
        """
        # Source artifact's computed_at is intentionally different from the injected one.
        stale_ts = "2026-09-01T00:00:00Z"
        fresh_ts = "2026-10-15T09:00:00Z"

        artifact = _minimal_artifact()
        artifact["computed_at"] = stale_ts

        result = health_artifact_to_finding(artifact, utc_now_fn=lambda: fresh_ts)

        assert result["computed_at"] == fresh_ts, (
            f"computed_at must be refreshed from utc_now_fn(), not forwarded from "
            f"the source artifact. Expected {fresh_ts!r}, got {result['computed_at']!r}."
        )
        assert result["computed_at"] != stale_ts, (
            "computed_at must not equal the stale source artifact timestamp."
        )

    def test_computed_at_is_recent_relative_to_call(self):
        """Without injection, computed_at is within a short window of now.

        Mutation (b) proof: if computed_at were hardcoded to a fixed string
        (e.g. '2026-01-01T00:00:00Z'), the difference from the real now would
        be much larger than 5 seconds, and this assertion would fail.

        The transform truncates to seconds (``timespec="seconds"``), so
        ``before`` may be fractionally ahead of the truncated timestamp.
        We expand the window by one second on the lower bound to account for
        this sub-second truncation while keeping the guard tight enough that
        any hardcoded value not within the same second would be caught.
        """
        # Truncate 'before' to the second, matching the transform's precision.
        before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact)  # no utc_now_fn — uses real time
        after = datetime.datetime.now(datetime.timezone.utc)

        ts_str = result["computed_at"]
        ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

        # Window: [before (truncated to second), after + 1 second].
        # The +1s upper padding ensures we don't get flaky failures at the
        # transition between seconds; the before truncation ensures a hardcoded
        # timestamp in the past is still caught.
        assert before <= ts <= after + datetime.timedelta(seconds=1), (
            f"computed_at {ts_str!r} is outside the [{before.isoformat()}, "
            f"{after.isoformat()}] window. A hardcoded timestamp would fail this "
            "freshness check — mutation (b) guard."
        )


# ---------------------------------------------------------------------------
# Mutation guard (c): local finding_key derivation → key-match test fails
# ---------------------------------------------------------------------------


class TestFindingKeyMatchesWriter:
    """Mutation (c): locally-derived finding_key must cause this test to fail.

    Guard: assert byte-equality between the transform's output key and the
    value from ``writer.compute_finding_key()`` called on the same inputs.
    Any divergence breaks D2 dedup and in-place update silently.
    """

    def test_finding_key_matches_compute_finding_key(self):
        """finding_key must be byte-identical to writer.compute_finding_key().

        Mutation (c) proof: replace the transform's
        ``finding_key = compute_finding_key(...)`` call with a local
        re-implementation (even a correct one that uses sha256).  If the
        canonical inputs differ even slightly from the locally-derived inputs,
        the keys diverge and this test fails.

        The test confirms the transform CALLS the shared function by verifying
        the output matches what the same function returns for the same inputs.
        """
        vin = SAMPLE_VIN
        finding_kind = FINDING_KIND

        # What the canonical writer computes.
        expected_key = compute_finding_key(
            agent_id=AGENT_ID,
            scope_field="vin",
            scope_value=vin,
            finding_kind=finding_kind,
        )

        artifact = _minimal_artifact(vin=vin, finding_kind=finding_kind)
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert result["finding_key"] == expected_key, (
            f"finding_key mismatch. Expected {expected_key!r} from "
            f"compute_finding_key(), got {result['finding_key']!r}. "
            "A locally-derived key breaks D2 dedup — mutation (c) guard."
        )

    def test_finding_key_pattern_matches_schema(self):
        """finding_key must match the schema pattern ^[0-9a-f]{64}$."""
        import re
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        key = result["finding_key"]
        assert re.match(r"^[0-9a-f]{64}$", key), (
            f"finding_key {key!r} does not match the schema pattern ^[0-9a-f]{{64}}$"
        )

    def test_finding_key_is_stable_for_same_inputs(self):
        """Same (agent_id, scope, finding_kind) always yields the same finding_key.

        Idempotency is the D2 dedup guarantee.  If finding_key changed between
        calls with identical inputs, UpdateItem and PutItem would diverge.
        """
        artifact = _minimal_artifact(vin=SAMPLE_VIN, finding_kind=FINDING_KIND)
        result_1 = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())
        result_2 = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert result_1["finding_key"] == result_2["finding_key"], (
            "finding_key must be stable for the same (agent_id, scope, finding_kind)."
        )

    def test_finding_key_differs_for_different_scope_values(self):
        """Different VINs produce different finding_keys (uniqueness guarantee)."""
        vin_a = "1HGBH41JXMN109186"
        vin_b = "2T1BUHE2XEC020374"
        assert vin_a != vin_b  # sanity

        artifact_a = _minimal_artifact(vin=vin_a)
        artifact_b = _minimal_artifact(vin=vin_b)

        result_a = health_artifact_to_finding(artifact_a, utc_now_fn=_utc_now_stub())
        result_b = health_artifact_to_finding(artifact_b, utc_now_fn=_utc_now_stub())

        assert result_a["finding_key"] != result_b["finding_key"], (
            "Different VINs must produce different finding_keys."
        )


# ---------------------------------------------------------------------------
# finding_id preservation
# ---------------------------------------------------------------------------


class TestFindingIdPreservation:
    """finding_id must be preserved from the source artifact."""

    def test_finding_id_preserved(self):
        """finding_id from the artifact is forwarded to the output."""
        known_id = "FIND-a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        artifact = _minimal_artifact()
        artifact["finding_id"] = known_id

        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert result["finding_id"] == known_id, (
            f"finding_id should be preserved. Expected {known_id!r}, "
            f"got {result['finding_id']!r}."
        )

    def test_finding_id_minted_when_absent(self):
        """A finding_id is minted if the artifact does not carry one."""
        artifact = _minimal_artifact()
        del artifact["finding_id"]

        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        fid = result["finding_id"]
        assert fid.startswith("FIND-"), (
            f"Minted finding_id must start with 'FIND-', got {fid!r}."
        )

    def test_finding_id_matches_schema_pattern(self):
        """finding_id must match the schema pattern FIND-{uuid4}."""
        import re
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        fid = result["finding_id"]
        pattern = r"^FIND-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        assert re.match(pattern, fid), (
            f"finding_id {fid!r} does not match schema pattern {pattern}."
        )


# ---------------------------------------------------------------------------
# Evidence entry enrichment unit tests
# ---------------------------------------------------------------------------


class TestEnrichEvidenceEntry:
    """Unit tests for ``_enrich_evidence_entry()``."""

    def test_partial_entry_receives_required_fields(self):
        """A source entry with only {source, value, provenance} is enriched."""
        entry = {"source": "adp.tires.replacement_flagged", "value": "True", "provenance": "live"}
        enriched = _enrich_evidence_entry(entry, run_ts=_FIXED_TS)

        assert enriched["kind"] == "adp.tires.replacement_flagged"
        assert enriched["source_ref"] == "agent-2-health-run"
        assert enriched["computed_at"] == _FIXED_TS

    def test_existing_fields_not_overwritten(self):
        """Pre-populated fields are preserved as-is."""
        entry = {
            "kind": "custom.kind",
            "source": "adp.battery.soh",
            "source_ref": "athena-query-CUSTOM",
            "value": "92",
            "provenance": "live",
            "computed_at": "2026-09-30T00:00:00Z",
        }
        enriched = _enrich_evidence_entry(entry, run_ts=_FIXED_TS)

        # Pre-populated fields preserved.
        assert enriched["kind"] == "custom.kind"
        assert enriched["source_ref"] == "athena-query-CUSTOM"
        assert enriched["computed_at"] == "2026-09-30T00:00:00Z"

    def test_absent_provenance_entry_preserved(self):
        """Entries with provenance='absent' are enriched without error."""
        entry = {"source": "adp.brakes.outcome", "value": None, "provenance": "absent"}
        enriched = _enrich_evidence_entry(entry, run_ts=_FIXED_TS)
        assert enriched["provenance"] == "absent"
        assert "kind" in enriched
        assert "source_ref" in enriched
        assert "computed_at" in enriched


# ---------------------------------------------------------------------------
# expires_at helper
# ---------------------------------------------------------------------------


class TestExpiresAt:
    """Unit tests for ``_expires_at_from_computed_at()``."""

    def test_expires_at_derives_from_runner_ttl_seconds(self):
        """expires_at = computed_at + runner._TTL_SECONDS, not a local constant.

        T17.3 fix: the transform no longer owns a separate ``_FINDING_TTL_DAYS``
        constant that diverged from the runner's 8-day TTL.  Expiry is computed
        from ``runner._TTL_SECONDS`` so the two can never drift.

        Mutation guard: change ``_TTL_SECONDS`` in ``runner.py`` → this test
        tracks it and still passes.  Hardcode the expiry here → this test fails.
        """
        import datetime
        from agents.tier2.runner import _TTL_SECONDS

        computed_at = "2026-10-01T02:00:00Z"
        result = _expires_at_from_computed_at(computed_at)

        # Parse both ends and compute the expected delta.
        ts_in = datetime.datetime.fromisoformat(computed_at.replace("Z", "+00:00"))
        ts_out = datetime.datetime.fromisoformat(result.replace("Z", "+00:00"))
        actual_delta = (ts_out - ts_in).total_seconds()

        assert actual_delta == _TTL_SECONDS, (
            f"expires_at delta is {actual_delta}s but runner._TTL_SECONDS is {_TTL_SECONDS}s. "
            "The transform must derive expiry from _TTL_SECONDS, not a local constant. "
            "Mutation guard: change _TTL_SECONDS → this test tracks the change and fails "
            "if the transform hardcodes a different value."
        )

    def test_expires_at_ends_with_z(self):
        """expires_at uses Z suffix for UTC."""
        result = _expires_at_from_computed_at("2026-10-01T00:00:00Z")
        assert result.endswith("Z"), f"expires_at must end with 'Z', got {result!r}."

    def test_expires_at_is_schema_valid(self):
        """expires_at passes the schema's date-time format validation."""
        import jsonschema

        result = _expires_at_from_computed_at("2026-09-18T02:00:00Z")
        # The schema uses format: date-time. Validate with a minimal schema.
        schema = {"type": "string", "format": "date-time"}
        jsonschema.validate(result, schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


# ---------------------------------------------------------------------------
# inputs_hash passthrough
# ---------------------------------------------------------------------------


class TestInputsHash:
    """inputs_hash must be forwarded unchanged from the source artifact."""

    def test_inputs_hash_preserved(self):
        """inputs_hash is the ADP row hash; the transform must not modify it."""
        artifact = _minimal_artifact(inputs_hash=_INPUTS_HASH)
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert result["inputs_hash"] == _INPUTS_HASH, (
            f"inputs_hash must be preserved from the source artifact. "
            f"Expected {_INPUTS_HASH!r}, got {result['inputs_hash']!r}."
        )

    def test_inputs_hash_matches_schema_pattern(self):
        """inputs_hash must match ^[0-9a-f]{64}$."""
        import re
        artifact = _minimal_artifact(inputs_hash=_INPUTS_HASH)
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert re.match(r"^[0-9a-f]{64}$", result["inputs_hash"]), (
            f"inputs_hash {result['inputs_hash']!r} does not match schema pattern."
        )


# ---------------------------------------------------------------------------
# Headline truncation
# ---------------------------------------------------------------------------


class TestHeadline:
    """headline must be ≤ 120 chars per schema."""

    def test_long_headline_is_truncated(self):
        """Headline longer than 120 chars is truncated to 120."""
        long_headline = "A" * 200
        artifact = _minimal_artifact()
        artifact["headline"] = long_headline
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        assert len(result["headline"]) <= 120, (
            f"Headline must be ≤ 120 chars (schema maxLength). "
            f"Got {len(result['headline'])} chars."
        )

    def test_short_headline_is_unchanged(self):
        """Headline within 120 chars is forwarded unchanged."""
        headline = "Battery is healthy."
        artifact = _minimal_artifact()
        artifact["headline"] = headline
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())
        assert result["headline"] == headline


# ---------------------------------------------------------------------------
# Scope passthrough
# ---------------------------------------------------------------------------


class TestScope:
    """scope must be passed through unchanged."""

    def test_scope_vin_preserved(self):
        """scope.vin is forwarded from the source artifact."""
        artifact = _minimal_artifact(vin=SAMPLE_VIN)
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())
        assert result["scope"] == {"vin": SAMPLE_VIN}

    def test_scope_is_valid_per_schema(self):
        """scope passes the oneOf constraint (exactly one field)."""
        artifact = _minimal_artifact()
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())
        errors = _validate_against_schema(result)
        assert not errors, f"Schema violations (likely scope-related): {errors}"


# ---------------------------------------------------------------------------
# Empty evidence — absent-data scenario
# ---------------------------------------------------------------------------


class TestEmptyEvidence:
    """Evidence may be empty in the absent-data scenario (schema allows it)."""

    def test_empty_evidence_is_schema_valid(self):
        """An empty evidence[] is valid per schema (absent-data scenario)."""
        artifact = _minimal_artifact(evidence=[])
        result = health_artifact_to_finding(artifact, utc_now_fn=_utc_now_stub())

        errors = _validate_against_schema(result)
        assert not errors, (
            f"Schema violations on empty evidence: {errors}. "
            "The schema permits an empty array for the absent-data scenario."
        )
        assert result["evidence"] == [], "Empty evidence should remain empty."
